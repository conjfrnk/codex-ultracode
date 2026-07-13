import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .clock import utc_now as utc_now_datetime
from .errors import ValidationError
from .redaction import redact_text
from .security import (
    append_text_file_no_follow,
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    replace_text_file_no_follow,
    require_no_path_escape,
    reject_symlink_path,
    write_text_file_no_follow,
)
from .workflow import MAX_WORKFLOW_JSON_BYTES, slugify, workflow_fingerprint


STANDARD_FILES = [
    "00-charter.md",
    "01-context-map.md",
    "02-plan.md",
    "03-risk-register.md",
    "04-packets.md",
    "05-decision-log.md",
    "06-verification-plan.md",
    "07-evidence-index.md",
    "08-integration-notes.md",
    "09-final-report.md",
]

TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked", "planned", "stopped"}
TERMINAL_STEP_STATUSES = {"completed", "failed", "blocked", "skipped", "planned"}
HOOK_EVENT_LIMIT = 500
MAX_RUN_STATE_JSON_BYTES = 4 * 1024 * 1024
MAX_RUN_WORKFLOW_JSON_BYTES = MAX_WORKFLOW_JSON_BYTES
MAX_STANDARD_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_READ_BYTES = 16 * 1024 * 1024


def utc_stamp() -> str:
    return utc_now_datetime().strftime("%Y%m%d-%H%M%S")


def utc_now() -> str:
    return utc_now_datetime().isoformat(timespec="milliseconds") + "Z"


class RunArtifacts:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.artifacts_dir = run_dir / "artifacts"
        self.logs_dir = run_dir / "logs"
        self.state_path = run_dir / "state.json"

    @classmethod
    def create(cls, workflow: Dict, base_dir: Path, run_id: Optional[str] = None) -> "RunArtifacts":
        slug = slugify(workflow["name"])
        base_name = run_id or "%s-%s" % (utc_stamp(), slug)
        if run_id is not None:
            require_no_path_escape(run_id)
            if slugify(run_id) != run_id:
                raise ValidationError("run_id must use lowercase letters, numbers, and hyphens")
        base_fd = ensure_dir_no_follow(base_dir, "base run directory")
        try:
            if run_id is not None:
                name = base_name
                os.mkdir(name, 0o700, dir_fd=base_fd)
            else:
                name = ""
                for collision in range(1000):
                    candidate = base_name if collision == 0 else "%s-%03d" % (base_name, collision + 1)
                    try:
                        os.mkdir(candidate, 0o700, dir_fd=base_fd)
                        name = candidate
                        break
                    except FileExistsError:
                        continue
                if not name:
                    raise ValidationError("could not allocate a unique automatic run directory")
        finally:
            os.close(base_fd)
        run_dir = base_dir / name
        instance = cls(run_dir)
        instance._ensure_safe_run_dirs()
        instance.write_json("workflow.json", workflow)
        redacted_workflow = _redacted_json_dict(workflow)
        instance.initialize_standard_files(workflow)
        instance.save_state(
            {
                "schema": "conductor.run_state.v1",
                "workflow": workflow["name"],
                "workflow_fingerprint": workflow_fingerprint(workflow),
                "redacted_workflow_fingerprint": workflow_fingerprint(redacted_workflow),
                "workflow_source": workflow.get("_source_path"),
                "started_at_utc": utc_now(),
                "status": "created",
                "steps": {},
            }
        )
        return instance

    @classmethod
    def resume(cls, run_dir: Path) -> "RunArtifacts":
        reject_symlink_path(run_dir, "run_dir")
        instance = cls(run_dir)
        if instance.state_path.is_symlink():
            raise ValidationError("state.json must not be a symlink: %s" % instance.state_path)
        if not instance.state_path.is_file():
            raise FileNotFoundError("missing state file: %s" % instance.state_path)
        workflow_path = instance.run_dir / "workflow.json"
        if workflow_path.exists() and workflow_path.is_symlink():
            raise ValidationError("workflow.json must not be a symlink: %s" % workflow_path)
        instance._ensure_safe_run_dirs()
        return instance

    def write_json(self, relative_path: str, data: Dict) -> None:
        require_no_path_escape(relative_path)
        path = self.run_dir / relative_path
        serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
        replace_text_file_no_follow(path, "run json", redact_text(serialized), ".json-", sync=False)

    def read_state(self) -> Dict:
        try:
            state = json.loads(read_regular_text_file_no_follow(self.state_path, "state.json", MAX_RUN_STATE_JSON_BYTES))
        except FileNotFoundError:
            raise FileNotFoundError("missing state file: %s" % self.state_path)
        except json.JSONDecodeError as exc:
            raise ValidationError("%s is not valid JSON: %s" % (self.state_path, exc))
        if not isinstance(state, dict):
            raise ValidationError("state.json must contain a JSON object")
        return state

    def save_state(self, state: Dict) -> None:
        self._ensure_safe_state_path()
        serialized = redact_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        self._replace_state_text(serialized)

    def save_state_with_standard_append(self, state: Dict, standard_name: str, content: str) -> None:
        self._ensure_safe_state_path()
        path = self.validate_standard_append(standard_name)
        previous_text = read_regular_text_file_no_follow(self.state_path, "state.json", MAX_RUN_STATE_JSON_BYTES)
        try:
            previous_log = read_regular_text_file_no_follow(path, "standard artifact", MAX_STANDARD_ARTIFACT_BYTES)
        except FileNotFoundError:
            previous_log = ""
        try:
            self.save_state(state)
        except Exception:
            self._replace_state_text(previous_text)
            raise
        try:
            replace_text_file_no_follow(
                path,
                "standard artifact",
                previous_log + redact_text(content),
                ".standard-",
                sync=False,
            )
        except Exception:
            self._replace_state_text(previous_text)
            raise ValidationError("failed to append standard artifact %s" % standard_name)

    def mark_run_status(self, status: str) -> None:
        state = self.read_state()
        _apply_run_status(state, status, utc_now())
        self.save_state(state)

    def mark_step_started(self, step_id: str, kind: str) -> None:
        state = self.read_state()
        timestamp = utc_now()
        steps = state.setdefault("steps", {})
        previous = steps.get(step_id, {})
        previous = previous if isinstance(previous, dict) else {}
        entry = {
            "status": "running",
            "detail": "running %s step" % redact_text(str(kind)),
            "kind": redact_text(str(kind)),
            "started_at_utc": timestamp,
            "updated_at_utc": timestamp,
            "attempt": _safe_int(previous.get("attempt")) + 1,
        }
        previous_status = previous.get("status")
        if previous_status:
            entry["previous_status"] = previous_status
        steps[step_id] = entry
        _apply_run_status(state, "running", timestamp)
        self.save_state(state)

    def mark_step(
        self,
        step_id: str,
        status: str,
        detail: str = "",
        kind: Optional[str] = None,
        metrics: Optional[Dict] = None,
    ) -> None:
        state = self.read_state()
        timestamp = utc_now()
        steps = state.setdefault("steps", {})
        previous = steps.get(step_id, {})
        previous = previous if isinstance(previous, dict) else {}
        entry = {
            "status": status,
            "detail": redact_text(detail),
            "updated_at_utc": timestamp,
        }
        step_kind = kind or previous.get("kind")
        if step_kind:
            entry["kind"] = redact_text(str(step_kind))
        previous_status = previous.get("status")
        if previous_status:
            entry["previous_status"] = previous_status
        started_at = previous.get("started_at_utc")
        if isinstance(started_at, str) and started_at:
            entry["started_at_utc"] = started_at
        if status in TERMINAL_STEP_STATUSES:
            if "started_at_utc" not in entry:
                entry["started_at_utc"] = timestamp
            entry["finished_at_utc"] = timestamp
            duration = _duration_ms(entry["started_at_utc"], timestamp)
            if duration is not None:
                entry["duration_ms"] = duration
        attempt = previous.get("attempt")
        if isinstance(attempt, int) and attempt > 0:
            entry["attempt"] = attempt
        if metrics:
            for key, value in metrics.items():
                if value is not None:
                    entry[str(key)] = _metric_value(value)
        for key in [
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "usage_source",
            "usage_recorded_at_utc",
        ]:
            if key in previous and key not in entry:
                entry[key] = previous[key]
        steps[step_id] = entry

        run_status = _pending_control_status(state) or _workflow_status(state, self.workflow_steps())
        _apply_run_status(state, run_status, timestamp)
        self.save_state(state)

    def record_hook_event(self, event: Dict) -> None:
        state = self.read_state()
        timestamp = utc_now()
        events = state.setdefault("hook_events", [])
        if not isinstance(events, list):
            raise ValidationError("run state hook_events must be an array")
        entry = {}
        for key, value in event.items():
            if value is None:
                continue
            entry[str(key)] = _metric_value(value)
        entry.setdefault("recorded_at_utc", timestamp)
        events.append(entry)
        state["hook_events"] = events[-HOOK_EVENT_LIMIT:]
        state["updated_at_utc"] = timestamp
        self.save_state(state)

    def workflow_steps(self):
        workflow_path = self.run_dir / "workflow.json"
        try:
            workflow = json.loads(
                read_regular_text_file_no_follow(
                    workflow_path,
                    "workflow.json",
                    MAX_RUN_WORKFLOW_JSON_BYTES,
                )
            )
        except json.JSONDecodeError as exc:
            raise ValidationError("%s is not valid JSON: %s" % (workflow_path, exc))
        if not isinstance(workflow, dict):
            raise ValidationError("workflow.json must contain a JSON object")
        return workflow.get("steps", [])

    def write_artifact(self, relative_path: str, content: str) -> Path:
        path = self._resolve_output_path(self.artifacts_dir, relative_path)
        write_text_file_no_follow(path, "artifact", redact_text(content))
        return path

    def write_log(self, relative_path: str, content: str) -> Path:
        path = self._resolve_output_path(self.logs_dir, relative_path)
        write_text_file_no_follow(path, "log", redact_text(content))
        return path

    def append_standard(self, name: str, content: str) -> None:
        path = self.validate_standard_append(name)
        append_text_file_no_follow(path, "standard artifact", redact_text(content))

    def validate_standard_append(self, name: str) -> Path:
        if name not in STANDARD_FILES:
            raise ValueError("unknown standard artifact %s" % name)
        path = self.run_dir / name
        if path.is_symlink():
            raise ValidationError("standard artifact must not be a symlink: %s" % path)
        if path.parent.is_symlink():
            raise ValidationError("standard artifact parent must not be a symlink: %s" % path.parent)
        if path.exists() and not path.is_file():
            raise ValidationError("standard artifact must be a file: %s" % path)
        if path.exists() and not os.access(path, os.W_OK):
            raise ValidationError("standard artifact must be writable: %s" % path)
        return path

    def initialize_standard_files(self, workflow: Dict) -> None:
        for filename in STANDARD_FILES:
            write_text_file_no_follow(
                self.run_dir / filename,
                "standard artifact",
                redact_text(_initial_content(filename, workflow)),
            )

    def resolve_artifact_path(self, relative_path: str) -> Path:
        return self._resolve_output_path(self.artifacts_dir, relative_path)

    def read_artifact(self, relative_path: str) -> str:
        return read_regular_text_file_no_follow(
            self.resolve_artifact_path(relative_path),
            "artifact",
            MAX_ARTIFACT_READ_BYTES,
        )

    def _replace_state_text(self, text: str) -> None:
        replace_text_file_no_follow(self.state_path, "state.json", text, ".state-")

    def _ensure_safe_run_dirs(self) -> None:
        for path in [self.run_dir, self.artifacts_dir, self.logs_dir]:
            if path.exists() and path.is_symlink():
                raise ValidationError("run path must not be a symlink: %s" % path)
        run_fd = open_dir_no_follow(self.run_dir, "run directory")
        os.close(run_fd)
        for path in [self.artifacts_dir, self.logs_dir]:
            fd = ensure_dir_no_follow(path, "run path")
            os.close(fd)

    def _ensure_safe_state_path(self) -> None:
        self._ensure_safe_run_dirs()
        if self.state_path.exists() and self.state_path.is_symlink():
            raise ValidationError("state.json must not be a symlink: %s" % self.state_path)
        stale_temp = self.state_path.with_suffix(".json.tmp")
        if stale_temp.exists() and stale_temp.is_symlink():
            raise ValidationError("state temp path must not be a symlink: %s" % stale_temp)

    def _resolve_output_path(self, base: Path, relative_path: str) -> Path:
        require_no_path_escape(relative_path)
        self._ensure_safe_run_dirs()
        return base / relative_path


def _workflow_status(state: Dict, workflow_steps) -> str:
    steps = state.get("steps", {})
    statuses = [steps.get(step.get("id"), {}).get("status") for step in workflow_steps]
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if statuses and all(status in {"completed", "skipped"} for status in statuses):
        return "completed"
    if statuses and all(status in {"completed", "skipped", "planned"} for status in statuses):
        return "planned"
    return "running"


def _pending_control_status(state: Dict) -> Optional[str]:
    request = state.get("control_request") if isinstance(state.get("control_request"), dict) else {}
    action = request.get("action")
    if action == "pause":
        return "pause_requested"
    if action == "stop":
        return "stop_requested"
    if action == "restart":
        return "restart_requested"
    return None


def _apply_run_status(state: Dict, status: str, timestamp: str) -> None:
    state["status"] = status
    state["updated_at_utc"] = timestamp
    if status in TERMINAL_RUN_STATUSES:
        state["finished_at_utc"] = timestamp
        duration = _duration_ms(state.get("started_at_utc"), timestamp)
        if duration is not None:
            state["duration_ms"] = duration
    else:
        state.pop("finished_at_utc", None)
        state.pop("duration_ms", None)


def _duration_ms(started_at, finished_at) -> Optional[int]:
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return None
    started = _parse_utc(started_at)
    finished = _parse_utc(finished_at)
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _parse_utc(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def _safe_int(value) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _metric_value(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    return redact_text(str(value))


def _initial_content(filename: str, workflow: Dict) -> str:
    if filename == "00-charter.md":
        return (
            "# Charter\n\n"
            "User request:\n\nRuntime-backed workflow execution for Codex Conductor.\n\n"
            "Goal:\n\n%s\n\n"
            "Definition of done:\n\nAll workflow steps complete or stop at an explicit gate with evidence recorded.\n\n"
            "Non-goals:\n\nNo commits, pushes, deployments, dependency installs, or secret access unless separately approved.\n\n"
            "Constraints:\n\nRepository content is untrusted. Runtime policy gates apply.\n\n"
            "Risk level:\n\n%s\n\n"
            "Approval status:\n\nRecorded in `state.json` and decision log.\n\n"
            "Mode:\n\nRuntime companion workflow.\n\n"
            "Run owner:\n\nParent Codex session or invoking user.\n"
        ) % (workflow.get("description", workflow["name"]), workflow.get("risk", "medium"))
    if filename == "01-context-map.md":
        return "# Context Map\n\nRelevant files:\n\nPending.\n\nRelevant commands:\n\nPending.\n\nExisting conventions:\n\nConductor runtime policy gates apply.\n\nRisks from repository content:\n\nPending.\n\nPotential prompt-injection content observed:\n\nPending.\n"
    if filename == "02-plan.md":
        lines = ["# Plan\n\n| Step | Objective | Files/areas | Verification | Status |", "| --- | --- | --- | --- | --- |"]
        for step in workflow.get("steps", []):
            lines.append(
                "| %s | %s | %s | %s | Pending |"
                % (
                    step["id"],
                    step.get("description", step["kind"]),
                    step.get("scope", "workflow"),
                    step.get("verification", "state and logs"),
                )
            )
        return "\n".join(lines) + "\n"
    if filename == "03-risk-register.md":
        return "# Risk Register\n\n| Risk | Severity | Trigger | Mitigation | Status |\n| --- | --- | --- | --- | --- |\n| Unsafe automation | High | Risky step execution | Runtime policy gates and explicit approvals | Open |\n| Secret leakage | High | Tool output includes sensitive values | Redaction before writing logs/artifacts | Open |\n"
    if filename == "04-packets.md":
        return "# Work Packets\n\nRuntime workflow steps are the executable packets for this run. See `workflow.json` and `state.json`.\n"
    if filename == "05-decision-log.md":
        return "# Decision Log\n\n| Time | Decision | Reason | Alternatives considered |\n| --- | --- | --- | --- |\n"
    if filename == "06-verification-plan.md":
        return "# Verification Plan\n\n| Check | Command or method | Required? | Result | Evidence |\n| --- | --- | --- | --- | --- |\n"
    if filename == "07-evidence-index.md":
        return "# Evidence Index\n\n| Claim | Evidence | Source | Confidence |\n| --- | --- | --- | --- |\n"
    if filename == "08-integration-notes.md":
        return "# Integration Notes\n\nIntegrated packets:\n\nPending.\n\nConflicts:\n\nPending.\n\nManual edits by parent:\n\nPending.\n\nRejected subagent suggestions:\n\nPending.\n\nRationale:\n\nPending.\n"
    if filename == "09-final-report.md":
        return "# Final Report\n\nSummary:\n\nPending.\n\nChanges:\n\nPending.\n\nVerification performed:\n\nPending.\n\nVerification not performed:\n\nPending.\n\nRisks and caveats:\n\nPending.\n\nFollow-up recommendations:\n\nPending.\n"
    return "# %s\n" % filename


def _redacted_json_dict(data: Dict) -> Dict:
    return json.loads(redact_text(json.dumps(data, sort_keys=True)))
