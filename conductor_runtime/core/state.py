"""External, hash-bound workflow state and artifacts."""

import fcntl
import hashlib
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, cast

from ..errors import ValidationError
from ..redaction import redact_text
from .policy import RuntimePolicy, policy_fingerprint
from .safe import (
    canonical_json_bytes,
    conductor_home,
    ensure_directory,
    external_runs_dir,
    load_json,
    read_regular_bytes,
    reject_symlink_components,
    replace_json,
    require_external_state_path,
    require_relative,
    resolve_under,
    sha256_bytes,
    strict_json_bytes,
    write_new_bytes,
    write_new_json,
)
from .workflow import slugify, validate_workflow, workflow_fingerprint


RUN_SCHEMA = "conductor.core_run.v1"
STATE_SCHEMA = "conductor.core_run_state.v1"
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_ARTIFACTS = 10000
MAX_EVENTS = 2000
TERMINAL_STATUSES = {"completed", "failed", "blocked", "paused", "planned", "stopped", "terminated"}
STEP_STATUSES = {"pending", "running", "completed", "failed", "blocked", "skipped", "planned"}
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
STEP_RECORD_FIELDS = {
    "kind",
    "status",
    "attempt",
    "started_at_utc",
    "finished_at_utc",
    "detail",
    "metrics",
    "outputs",
    "resume_session_id",
    "resume_available",
    "session_id_sha256",
    "stage_evidence",
    "pending_stage",
    "packets",
    "source_outputs",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def workspace_apply_lock(workspace: Path):
    """Serialize source mutations using the canonical workspace identity."""
    requested_source = Path(workspace).expanduser()
    reject_symlink_components(requested_source, "workspace apply lock source")
    source = requested_source.resolve()
    lock_root = require_external_state_path(
        conductor_home() / "locks",
        source,
        "workspace apply lock directory",
    )
    lock_root = ensure_directory(lock_root, "workspace apply lock directory")
    identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
    path = lock_root / ("stage-apply-%s.lock" % identity)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValidationError("cannot open workspace apply lock") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError("workspace apply lock must be a private regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            observed = path.lstat()
        except OSError as exc:
            raise ValidationError("cannot inspect workspace apply lock") from exc
        if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
            raise ValidationError("workspace apply lock changed while acquiring it")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class RunState:
    def __init__(self, run_dir: Path, descriptor: Dict, state: Dict):
        self.run_dir = Path(run_dir)
        self.descriptor = descriptor
        self.state = state
        self._mutex = threading.RLock()

    @classmethod
    def inspect(cls, run_dir: Path):
        directory = Path(run_dir).expanduser()
        descriptor = load_json(directory / "run.json", "run descriptor", MAX_STATE_BYTES)
        state = load_json(directory / "state.json", "run state", MAX_STATE_BYTES)
        _validate_hashed(descriptor, "descriptor_sha256", RUN_SCHEMA, "run descriptor")
        _validate_hashed(state, "state_sha256", STATE_SCHEMA, "run state")
        workflow_payload = read_regular_bytes(directory / "workflow.json", "run workflow", 2 * 1024 * 1024)
        if sha256_bytes(workflow_payload) != descriptor.get("workflow_sha256"):
            raise ValidationError("run workflow snapshot hash does not match")
        workflow = strict_json_bytes(workflow_payload, "run workflow")
        validate_workflow(workflow)
        _validate_run_shapes(descriptor, state, workflow)
        if descriptor.get("run_id") != state.get("run_id"):
            raise ValidationError("run descriptor and state identities differ")
        instance = cls(directory, descriptor, state)
        instance.verify_recorded_artifacts()
        return instance

    @classmethod
    def create(
        cls,
        workflow: Dict,
        workspace: Path,
        policy: RuntimePolicy,
        *,
        runs_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
        context_sha256: Optional[str] = None,
    ):
        public = {key: value for key, value in workflow.items() if not key.startswith("_")}
        validate_workflow(public)
        workspace_path = Path(workspace).resolve()
        requested_root = Path(runs_dir).expanduser().resolve(strict=False) if runs_dir else external_runs_dir(workspace_path)
        requested_root = require_external_state_path(requested_root, workspace_path, "runs directory")
        root = ensure_directory(requested_root, "runs directory")
        identifier = _new_run_id(workflow["name"], run_id)
        run_dir = root / identifier
        try:
            os.mkdir(run_dir, 0o700)
        except FileExistsError as exc:
            raise ValidationError("run directory already exists") from exc
        artifacts_dir = run_dir / "artifacts"
        os.mkdir(artifacts_dir, 0o700)
        workflow_bytes = canonical_json_bytes(public)
        workflow_sha256 = sha256_bytes(workflow_bytes)
        workspace_sha256 = hashlib.sha256(str(workspace_path).encode("utf-8")).hexdigest()
        created = utc_now()
        descriptor = {
            "schema": RUN_SCHEMA,
            "run_id": identifier,
            "workflow_name": workflow["name"],
            "workflow_sha256": workflow_sha256,
            "workspace_sha256": workspace_sha256,
            "policy_sha256": policy_fingerprint(policy),
            "context_sha256": context_sha256 or sha256_bytes(b""),
            "created_at_utc": created,
            "workflow_path": "workflow.json",
            "state_path": "state.json",
            "artifacts_path": "artifacts",
        }
        descriptor = _with_hash(descriptor, "descriptor_sha256")
        state = {
            "schema": STATE_SCHEMA,
            "generation": 0,
            "status": "pending",
            "run_id": identifier,
            "workflow_sha256": workflow_sha256,
            "workspace_sha256": workspace_sha256,
            "policy_sha256": descriptor["policy_sha256"],
            "context_sha256": descriptor["context_sha256"],
            "created_at_utc": created,
            "updated_at_utc": created,
            "steps": {
                step["id"]: {
                    "kind": step["kind"],
                    "status": "pending",
                    "attempt": 0,
                }
                for step in workflow["steps"]
            },
            "artifacts": {},
            "events": [],
        }
        state = _with_hash(state, "state_sha256")
        write_new_bytes(run_dir / "workflow.json", workflow_bytes, "run workflow")
        write_new_json(run_dir / "run.json", descriptor, "run descriptor")
        write_new_json(run_dir / "state.json", state, "run state")
        return cls(run_dir, descriptor, state)

    @classmethod
    def resume(
        cls,
        run_dir: Path,
        workflow: Dict,
        workspace: Path,
        policy: RuntimePolicy,
        context_sha256: Optional[str] = None,
    ):
        directory = require_external_state_path(run_dir, workspace, "run directory")
        descriptor = load_json(directory / "run.json", "run descriptor", MAX_STATE_BYTES)
        state = load_json(directory / "state.json", "run state", MAX_STATE_BYTES)
        _validate_hashed(descriptor, "descriptor_sha256", RUN_SCHEMA, "run descriptor")
        _validate_hashed(state, "state_sha256", STATE_SCHEMA, "run state")
        _validate_run_shapes(descriptor, state, workflow)
        if descriptor["run_id"] != state["run_id"]:
            raise ValidationError("run descriptor and state identities differ")
        public = {key: value for key, value in workflow.items() if not key.startswith("_")}
        expected_workflow = workflow_fingerprint(public)
        snapshot = read_regular_bytes(directory / "workflow.json", "run workflow", 2 * 1024 * 1024)
        if sha256_bytes(snapshot) != expected_workflow or descriptor["workflow_sha256"] != expected_workflow:
            raise ValidationError("resume workflow does not match the original run")
        expected_workspace = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()
        if descriptor["workspace_sha256"] != expected_workspace or state["workspace_sha256"] != expected_workspace:
            raise ValidationError("resume workspace does not match the original run")
        expected_policy = policy_fingerprint(policy)
        if descriptor["policy_sha256"] != expected_policy or state["policy_sha256"] != expected_policy:
            raise ValidationError("resume permissions or approvals changed")
        expected_context = context_sha256 or sha256_bytes(b"")
        if descriptor.get("context_sha256") != expected_context or state.get("context_sha256") != expected_context:
            raise ValidationError("resume iteration context changed")
        instance = cls(directory, descriptor, state)
        instance.verify_recorded_artifacts()
        return instance

    @contextmanager
    def lock(self):
        path = self.run_dir / ".lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValidationError("run is already active") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def reload(self) -> Dict:
        with self._mutex:
            state = load_json(self.run_dir / "state.json", "run state", MAX_STATE_BYTES)
            _validate_hashed(state, "state_sha256", STATE_SCHEMA, "run state")
            workflow_payload = read_regular_bytes(self.run_dir / "workflow.json", "run workflow", 2 * 1024 * 1024)
            workflow = strict_json_bytes(workflow_payload, "run workflow")
            validate_workflow(workflow)
            _validate_run_shapes(self.descriptor, state, workflow)
            if state["run_id"] != self.descriptor["run_id"]:
                raise ValidationError("run state identity changed")
            self.state = state
            return state

    def save(self) -> None:
        with self._mutex:
            state = dict(self.state)
            state["generation"] = int(state.get("generation", 0)) + 1
            state["updated_at_utc"] = utc_now()
            state = _with_hash(state, "state_sha256")
            replace_json(self.run_dir / "state.json", state, "run state")
            self.state = state

    def transition_step(self, step_id: str, status: str, *, detail: str = "", metrics=None) -> None:
        with self._mutex:
            if step_id not in self.state["steps"] or status not in STEP_STATUSES:
                raise ValidationError("invalid step transition")
            record = dict(self.state["steps"][step_id])
            previous = record["status"]
            if status == "running":
                resumable_interruption = previous == "running" and isinstance(
                    record.get("pending_stage"),
                    str,
                )
                if previous not in {"pending", "failed", "blocked", "planned"} and not resumable_interruption:
                    raise ValidationError("step %s cannot start from %s" % (step_id, previous))
                record["attempt"] = int(record.get("attempt", 0)) + 1
                record["started_at_utc"] = utc_now()
                record.pop("finished_at_utc", None)
                record.pop("metrics", None)
                packets = record.get("packets")
                if isinstance(packets, dict):
                    cleaned_packets = {}
                    for packet_id, packet in packets.items():
                        if isinstance(packet, dict) and "result_metrics" in packet:
                            packet = dict(packet)
                            packet.pop("result_metrics", None)
                        cleaned_packets[packet_id] = packet
                    record["packets"] = cleaned_packets
            elif status in {"completed", "failed", "blocked", "skipped", "planned"}:
                record["finished_at_utc"] = utc_now()
            record["status"] = status
            if detail:
                record["detail"] = redact_text(detail)[:1000]
            else:
                record.pop("detail", None)
            if metrics is not None:
                record["metrics"] = metrics
            self.state["steps"][step_id] = record
            self._event("step", step_id, status)
            self.save()

    def update_step(self, step_id: str, **fields) -> None:
        with self._mutex:
            if step_id not in self.state["steps"]:
                raise ValidationError("unknown run step")
            record = dict(self.state["steps"][step_id])
            record.update(fields)
            self.state["steps"][step_id] = record
            self.save()

    def set_status(self, status: str, detail: str = "") -> None:
        with self._mutex:
            if status not in TERMINAL_STATUSES | {"pending", "running"}:
                raise ValidationError("invalid run status")
            self.state["status"] = status
            if status in TERMINAL_STATUSES:
                self.state["finished_at_utc"] = utc_now()
            if detail:
                self.state["detail"] = redact_text(detail)[:1000]
            self._event("run", "", status)
            self.save()

    def artifact_path(self, relative: str) -> Path:
        return resolve_under(self.run_dir / "artifacts", relative, "artifact path")

    def write_artifact(self, relative: str, payload, *, replace: bool = False) -> Dict:
        with self._mutex:
            clean = require_relative(relative, "artifact path")
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            if not isinstance(data, bytes) or len(data) > MAX_ARTIFACT_BYTES:
                raise ValidationError("artifact payload is invalid or oversized")
            path = self.artifact_path(clean)
            if replace and path.exists():
                from .safe import replace_bytes

                replace_bytes(path, data, "run artifact")
            else:
                write_new_bytes(path, data, "run artifact")
            record = {"sha256": sha256_bytes(data), "size_bytes": len(data)}
            self.state["artifacts"][clean] = record
            self.save()
            return record

    def read_artifact(self, relative: str, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
        clean = require_relative(relative, "artifact path")
        record = self.state["artifacts"].get(clean)
        if not isinstance(record, dict):
            raise ValidationError("artifact %s is not recorded" % clean)
        payload = read_regular_bytes(self.artifact_path(clean), "run artifact", max_bytes)
        if record != {"sha256": sha256_bytes(payload), "size_bytes": len(payload)}:
            raise ValidationError("artifact %s changed after it was recorded" % clean)
        return payload

    def verify_recorded_artifacts(self) -> None:
        artifacts = self.state.get("artifacts")
        if not isinstance(artifacts, dict) or len(artifacts) > MAX_ARTIFACTS:
            raise ValidationError("run artifact index is invalid")
        for relative in artifacts:
            self.read_artifact(relative)
        # Result containers are optional so legacy runs remain valid. When
        # present, they are part of the run's integrity surface and every
        # state-held opaque id must resolve under the same descriptor.
        from .results import RunResultStore

        RunResultStore(self).verify()

    def _event(self, kind: str, step_id: str, status: str) -> None:
        events = list(self.state.get("events", []))
        if len(events) >= MAX_EVENTS:
            events = events[-(MAX_EVENTS - 1) :]
        events.append(
            {
                "sequence": int(self.state.get("generation", 0)) + 1,
                "at_utc": utc_now(),
                "kind": kind,
                "step_id": step_id,
                "status": status,
            }
        )
        self.state["events"] = events


def _new_run_id(workflow_name: str, requested: Optional[str]) -> str:
    if requested is not None:
        if not isinstance(requested, str) or RUN_ID.fullmatch(requested) is None:
            raise ValidationError("run id must be lowercase letters, digits, and hyphens")
        return requested
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "%s-%s-%s" % (timestamp, slugify(workflow_name)[:48], secrets.token_hex(4))


def _with_hash(value: Dict, field: str) -> Dict:
    result = dict(value)
    result.pop(field, None)
    result[field] = sha256_bytes(canonical_json_bytes(result))
    return result


def _validate_hashed(value, field: str, schema: str, label: str) -> None:
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValidationError("%s schema is invalid" % label)
    observed = value.get(field)
    if not isinstance(observed, str) or len(observed) != 64:
        raise ValidationError("%s hash is missing" % label)
    expected = _with_hash(value, field)[field]
    if observed != expected:
        raise ValidationError("%s hash does not match" % label)


def _validate_run_shapes(descriptor: Dict, state: Dict, workflow: Dict) -> None:
    descriptor_fields = {
        "schema",
        "run_id",
        "workflow_name",
        "workflow_sha256",
        "workspace_sha256",
        "policy_sha256",
        "context_sha256",
        "created_at_utc",
        "workflow_path",
        "state_path",
        "artifacts_path",
        "descriptor_sha256",
    }
    if set(descriptor) != descriptor_fields:
        raise ValidationError("run descriptor fields are invalid")
    if RUN_ID.fullmatch(descriptor.get("run_id", "")) is None:
        raise ValidationError("run descriptor id is invalid")
    for field in ("workflow_sha256", "workspace_sha256", "policy_sha256", "context_sha256"):
        value = descriptor.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValidationError("run descriptor %s is invalid" % field)
    if (
        descriptor.get("workflow_name") != workflow.get("name")
        or descriptor.get("workflow_path") != "workflow.json"
        or descriptor.get("state_path") != "state.json"
        or descriptor.get("artifacts_path") != "artifacts"
    ):
        raise ValidationError("run descriptor paths or workflow identity are invalid")
    _bounded_timestamp(descriptor.get("created_at_utc"), "run descriptor timestamp")
    required_state = {
        "schema",
        "generation",
        "status",
        "run_id",
        "workflow_sha256",
        "workspace_sha256",
        "policy_sha256",
        "context_sha256",
        "created_at_utc",
        "updated_at_utc",
        "steps",
        "artifacts",
        "events",
        "state_sha256",
    }
    if not required_state.issubset(state) or set(state) - required_state - {"finished_at_utc", "detail"}:
        raise ValidationError("run state fields are invalid")
    if state.get("run_id") != descriptor["run_id"]:
        raise ValidationError("run state identity is invalid")
    for field in ("workflow_sha256", "workspace_sha256", "policy_sha256", "context_sha256"):
        if state.get(field) != descriptor[field]:
            raise ValidationError("run state %s binding is invalid" % field)
    if (
        isinstance(state.get("generation"), bool)
        or not isinstance(state.get("generation"), int)
        or state["generation"] < 0
        or not isinstance(state.get("status"), str)
        or state.get("status") not in TERMINAL_STATUSES | {"pending", "running"}
    ):
        raise ValidationError("run state generation or status is invalid")
    _bounded_timestamp(state.get("created_at_utc"), "run created timestamp")
    _bounded_timestamp(state.get("updated_at_utc"), "run updated timestamp")
    if "finished_at_utc" in state:
        _bounded_timestamp(state["finished_at_utc"], "run finished timestamp")
    if "detail" in state and (not isinstance(state["detail"], str) or len(state["detail"]) > 1000):
        raise ValidationError("run state detail is invalid")
    steps = state.get("steps")
    expected_steps = {step["id"]: step["kind"] for step in workflow["steps"]}
    if not isinstance(steps, dict) or set(steps) != set(expected_steps):
        raise ValidationError("run state step identities are invalid")
    for step_id, record in steps.items():
        if not isinstance(record, dict) or record.get("kind") != expected_steps[step_id]:
            raise ValidationError("run state step record is invalid")
        if not isinstance(record.get("status"), str) or record.get("status") not in STEP_STATUSES:
            raise ValidationError("run state step status is invalid")
        attempt = record.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 <= attempt <= 1000:
            raise ValidationError("run state step attempt is invalid")
        _validate_step_record(record)
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict) or len(artifacts) > MAX_ARTIFACTS:
        raise ValidationError("run artifact index is invalid")
    for relative, record in artifacts.items():
        require_relative(relative, "run artifact index path")
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "size_bytes"}
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
            or isinstance(record.get("size_bytes"), bool)
            or not isinstance(record.get("size_bytes"), int)
            or not 0 <= record["size_bytes"] <= MAX_ARTIFACT_BYTES
        ):
            raise ValidationError("run artifact index record is invalid")
    events = state.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValidationError("run event history is invalid")
    previous_sequence = 0
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event) != {"sequence", "at_utc", "kind", "step_id", "status"}
            or not isinstance(event.get("kind"), str)
            or event.get("kind") not in {"run", "step"}
            or isinstance(event.get("sequence"), bool)
            or not isinstance(event.get("sequence"), int)
            or event["sequence"] < 1
        ):
            raise ValidationError("run event record is invalid")
        sequence = event["sequence"]
        kind = event["kind"]
        if (
            sequence <= previous_sequence
            or sequence > state["generation"]
            or not isinstance(event.get("at_utc"), str)
            or not 0 < len(event["at_utc"]) <= 64
            or (
                kind == "run"
                and (
                    event.get("step_id") != ""
                    or not isinstance(event.get("status"), str)
                    or event.get("status") not in TERMINAL_STATUSES | {"pending", "running"}
                )
            )
            or (
                kind == "step"
                and (
                    not isinstance(event.get("step_id"), str)
                    or event.get("step_id") not in steps
                    or not isinstance(event.get("status"), str)
                    or event.get("status") not in STEP_STATUSES
                )
            )
        ):
            raise ValidationError("run event binding is invalid")
        previous_sequence = sequence


def _validate_step_record(record: Dict) -> None:
    if set(record) - STEP_RECORD_FIELDS:
        raise ValidationError("run state step fields are invalid")
    for field in ("started_at_utc", "finished_at_utc"):
        if field in record:
            _bounded_timestamp(record[field], "step timestamp")
    if "detail" in record and (not isinstance(record["detail"], str) or len(record["detail"]) > 1000):
        raise ValidationError("run state step detail is invalid")
    outputs = record.get("outputs")
    if outputs is not None:
        if (
            not isinstance(outputs, list)
            or len(outputs) > MAX_ARTIFACTS
            or not all(isinstance(relative, str) for relative in outputs)
            or len(outputs) != len(set(outputs))
        ):
            raise ValidationError("run state step outputs are invalid")
        for relative in outputs:
            require_relative(relative, "run state step output")
    if "metrics" in record:
        metrics = record["metrics"]
        if not isinstance(metrics, dict) or len(canonical_json_bytes(metrics)) > MAX_STATE_BYTES:
            raise ValidationError("run state step metrics are invalid")
    resume_session_id = record.get("resume_session_id")
    if resume_session_id is not None and (
        not isinstance(resume_session_id, str) or not resume_session_id or len(resume_session_id) > 200
    ):
        raise ValidationError("run state resume session is invalid")
    if "resume_available" in record and not isinstance(record["resume_available"], bool):
        raise ValidationError("run state resume availability is invalid")
    session_hash = record.get("session_id_sha256")
    if session_hash is not None and (
        not isinstance(session_hash, str) or re.fullmatch(r"[0-9a-f]{64}", session_hash) is None
    ):
        raise ValidationError("run state session hash is invalid")
    if "stage_evidence" in record:
        require_relative(record["stage_evidence"], "run state stage evidence")
    pending_stage = record.get("pending_stage")
    if pending_stage is not None:
        require_relative(pending_stage, "run state pending stage")
    source_outputs = record.get("source_outputs")
    if source_outputs is not None and (
        isinstance(source_outputs, bool)
        or not isinstance(source_outputs, int)
        or not 0 <= source_outputs <= MAX_ARTIFACTS
    ):
        raise ValidationError("run state source output count is invalid")
    if "packets" in record:
        _validate_packet_records(record["packets"])


def _validate_packet_records(value) -> None:
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValidationError("run state map packet index is invalid")
    for key, packet in value.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[0-9]{4}", key) is None
            or not 1 <= int(key) <= 1000
        ):
            raise ValidationError("run state map packet id is invalid")
        if not isinstance(packet, dict):
            raise ValidationError("run state map packet record is invalid")
        if packet.get("status") == "failed":
            fields = {"status", "error_class"}
            if "resume_session_id" in packet:
                fields.add("resume_session_id")
            if "result_metrics" in packet:
                fields.add("result_metrics")
            if set(packet) != fields:
                raise ValidationError("run state failed map packet fields are invalid")
            error_class = packet.get("error_class")
            resume_session_id = packet.get("resume_session_id")
            if (
                not isinstance(error_class, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_class) is None
                or (
                    resume_session_id is not None
                    and (
                        not isinstance(resume_session_id, str)
                        or not resume_session_id
                        or len(resume_session_id) > 200
                    )
                )
            ):
                raise ValidationError("run state failed map packet is invalid")
            result_metrics = packet.get("result_metrics")
            if result_metrics is not None and (
                not isinstance(result_metrics, dict)
                or len(canonical_json_bytes(result_metrics)) > MAX_STATE_BYTES
            ):
                raise ValidationError("run state failed map packet result metrics are invalid")
            continue
        fields = {
            "status",
            "output",
            "output_sha256",
            "charged_tokens",
            "cap_tokens",
            "session_id_sha256",
            "receipt",
        }
        if set(packet) != fields or packet.get("status") != "completed":
            raise ValidationError("run state completed map packet fields are invalid")
        charged = packet.get("charged_tokens")
        cap = packet.get("cap_tokens")
        if (
            isinstance(charged, bool)
            or not isinstance(charged, int)
            or isinstance(cap, bool)
            or not isinstance(cap, int)
            or not 0 <= charged <= cap <= 10**9
            or cap < 100
            or not isinstance(packet.get("output_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", packet["output_sha256"]) is None
            or not isinstance(packet.get("session_id_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", packet["session_id_sha256"]) is None
        ):
            raise ValidationError("run state completed map packet is invalid")
        require_relative(cast(str, packet.get("output")), "run state map packet output")
        require_relative(cast(str, packet.get("receipt")), "run state map packet receipt")


def _bounded_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise ValidationError("%s is invalid" % label)
