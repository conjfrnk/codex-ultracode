"""Lean workflow runner with bounded map, context, verification, and resume."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional, cast

from ..errors import ConductorError, PolicyError, StepExecutionError, ValidationError
from ..redaction import redact_text
from .codex import CodexInvocationError, invoke_codex
from .policy import (
    RuntimePolicy,
    enforce_agent,
    enforce_shell,
    enforce_workflow_risk,
    prepare_shell_launch,
    require_approval,
)
from .process import run_process, sanitized_subprocess_environment
from .safe import (
    canonical_json_bytes,
    read_regular_text,
    reject_symlink_components,
    resolve_under,
    sha256_bytes,
    strict_json_bytes,
)
from .staged import (
    create_stage,
    discard_stage,
    finalize_stage,
    load_pending_stage,
    mark_stage_verified,
    pending_stage_descriptor,
    snapshot_workspace,
    validate_stage_workspace,
    validate_stage_evidence,
)
from .state import RunState
from .workflow import (
    MAX_CONTEXT_SOURCES,
    MAX_ITEMS,
    staged_verifier_ids,
    validate_map_item,
    validate_workflow,
)


DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_CONTEXT_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_EXCERPT_BYTES = 8 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_COLLECT_INPUT_BYTES = 16 * 1024 * 1024
MAX_ITERATION_CONTEXT_CHARS = 8000
CONTEXT_BEGIN = "BEGIN_UNTRUSTED_DEPENDENCY_EVIDENCE"
CONTEXT_END = "END_UNTRUSTED_DEPENDENCY_EVIDENCE"
ITERATION_CONTEXT_BEGIN = "BEGIN_UNTRUSTED_PRIOR_VERIFIER_FEEDBACK"
ITERATION_CONTEXT_END = "END_UNTRUSTED_PRIOR_VERIFIER_FEEDBACK"
MAP_ITEM_BEGIN = "BEGIN_UNTRUSTED_MAP_ITEM"
MAP_ITEM_END = "END_UNTRUSTED_MAP_ITEM"


class WorkflowRunner:
    def __init__(
        self,
        workflow: Dict,
        workspace: Path,
        base_run_dir: Optional[Path],
        policy: RuntimePolicy,
        *,
        dry_run: bool = False,
        run_id: Optional[str] = None,
        resume_dir: Optional[Path] = None,
        max_workers: Optional[int] = None,
        iteration_context: Optional[str] = None,
    ):
        public = {key: value for key, value in workflow.items() if not key.startswith("_")}
        validate_workflow(public)
        self.workflow = public
        self.workspace = Path(workspace).resolve()
        reject_symlink_components(self.workspace, "workspace")
        if not self.workspace.is_dir():
            raise ValidationError("workspace must be a directory")
        self.policy = policy
        self.dry_run = bool(dry_run)
        configured_workers = workflow.get("max_workers", 1)
        requested_workers = configured_workers if max_workers is None else max_workers
        if not isinstance(requested_workers, int) or isinstance(requested_workers, bool) or requested_workers < 1:
            raise ValidationError("max_workers must be a positive integer")
        self.max_workers = min(configured_workers, requested_workers)
        if iteration_context is not None and (
            not isinstance(iteration_context, str) or len(iteration_context) > MAX_ITERATION_CONTEXT_CHARS
        ):
            raise ValidationError("iteration context is invalid or oversized")
        self.iteration_context = redact_text(iteration_context or "").strip()
        context_sha256 = sha256_bytes(self.iteration_context.encode("utf-8"))
        if resume_dir is not None:
            self.run = RunState.resume(
                resume_dir,
                public,
                self.workspace,
                policy,
                context_sha256=context_sha256,
            )
        else:
            self.run = RunState.create(
                public,
                self.workspace,
                policy,
                runs_dir=base_run_dir,
                run_id=run_id,
                context_sha256=context_sha256,
            )
        self._stage = self._load_stage_state()

    def execute(self) -> RunState:
        with self.run.lock():
            self.run.reload()
            if self.run.state["status"] == "completed":
                self.run.verify_recorded_artifacts()
                return self.run
            self.run.set_status("running")
            try:
                enforce_workflow_risk(self.workflow, self.policy)
            except PolicyError as exc:
                self.run.set_status("blocked", str(exc))
                return self.run
            for step in self.workflow["steps"]:
                record = self.run.state["steps"][step["id"]]
                if record["status"] == "completed":
                    self._verify_completed_step(step, record)
                    continue
                if not self._dependencies_complete(step):
                    self.run.transition_step(step["id"], "blocked", detail="dependencies are incomplete")
                    self.run.set_status("blocked", "workflow dependency blocked")
                    return self.run
                try:
                    if self.dry_run:
                        self._preflight_step(step)
                        self.run.transition_step(step["id"], "planned", detail="validated without execution")
                    else:
                        self.run.transition_step(step["id"], "running")
                        metrics = self._execute_step(step)
                        self.run.transition_step(step["id"], "completed", metrics=metrics)
                except PolicyError as exc:
                    self.run.transition_step(step["id"], "blocked", detail=str(exc))
                    self.run.set_status("blocked", str(exc))
                    return self.run
                except (ConductorError, OSError, ValueError) as exc:
                    failure: Exception = exc
                    resumable = isinstance(exc, CodexInvocationError) and exc.resumable
                    if step.get("sandbox", "read-only") == "workspace-write":
                        failure, resumable = self._handle_failed_stage(step, failure, resumable)
                    elif self._stage is not None and self._depends_on(step["id"], self._stage["step_id"]):
                        failure = self._discard_invalid_downstream_stage(failure)
                    if resumable and isinstance(failure, CodexInvocationError):
                        self.run.update_step(
                            step["id"],
                            resume_session_id=failure.session_id,
                            resume_available=True,
                        )
                    self.run.transition_step(step["id"], "failed", detail=str(failure))
                    self.run.set_status("failed", failure.__class__.__name__)
                    return self.run
            if self.dry_run:
                self.run.set_status("planned")
            else:
                try:
                    self._verify_and_seal_stage()
                except (ConductorError, OSError, ValueError) as exc:
                    self.run.set_status("failed", str(exc))
                    return self.run
                self.run.set_status("completed")
            return self.run

    def _execute_step(self, step: Dict) -> Dict:
        kind = step["kind"]
        if kind == "write_artifact":
            record = self.run.write_artifact(step["output"], step["content"])
            self.run.update_step(step["id"], outputs=[step["output"]])
            return {"output_sha256": record["sha256"], "output_bytes": record["size_bytes"]}
        if kind == "collect_results":
            return self._collect_results(step)
        if kind == "manual_gate":
            approval = step.get("approval_id", step["id"])
            require_approval(self.policy, approval, "manual gate %s" % step["id"])
            return {"approval_present": True, "approval_values_persisted": False}
        if kind == "shell":
            return self._shell(step)
        if kind == "codex_exec":
            return self._codex(step)
        if kind == "agent_map":
            return self._agent_map(step)
        raise ValidationError("unsupported step kind")

    def _preflight_step(self, step: Dict) -> None:
        kind = step["kind"]
        if kind == "manual_gate":
            require_approval(self.policy, step.get("approval_id", step["id"]), "manual gate")
        elif kind == "shell":
            source = self._active_workspace(step)
            cwd = self._shell_workspace(step, source)
            enforce_shell(
                step,
                self.policy,
                cwd=cwd,
                workspace=source,
                workspace_alias=self.workspace,
                environment=sanitized_subprocess_environment(),
            )
        elif kind == "codex_exec":
            enforce_agent(step, self.policy)
            self._load_prompt(step, allow_missing_artifact=True)
        elif kind == "agent_map":
            workers = min(step.get("max_workers", self.max_workers), self.max_workers)
            enforce_agent(step, self.policy, workers=workers)
            if step.get("items") is not None:
                self._map_packets(step)

    def _shell(self, step: Dict) -> Dict:
        source = self._active_workspace(step)
        resolution_cwd = self._shell_workspace(step, source)
        environment = sanitized_subprocess_environment()
        assessment = enforce_shell(
            step,
            self.policy,
            cwd=resolution_cwd,
            workspace=source,
            workspace_alias=self.workspace,
            environment=environment,
        )
        attempt = self.run.state["steps"][step["id"]]["attempt"]
        sandbox_id = "shell-%s-attempt-%d" % (step["id"], attempt)
        isolated = create_stage(self.run.run_dir, sandbox_id, source)
        mutated = False
        try:
            cwd = self._shell_workspace(step, isolated["stage_dir"])
            launch_argv = prepare_shell_launch(
                assessment,
                isolated_workspace=isolated["stage_dir"],
            )
            timeout = step.get("timeout_seconds", self.workflow.get("default_timeout_seconds", 300))
            limit = step.get("output_limit_bytes", self.workflow.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES))
            result = run_process(
                launch_argv,
                cwd=cwd,
                timeout_seconds=timeout,
                output_limit_bytes=limit,
                env=environment,
            )
            capture = step.get("capture")
            if capture:
                mode = step.get("capture_mode", "combined")
                if mode == "stdout":
                    text = result.stdout
                elif mode == "stderr":
                    text = result.stderr
                else:
                    text = result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr
                record = self.run.write_artifact(capture, redact_text(text), replace=True)
                self.run.update_step(step["id"], outputs=[capture])
            else:
                record = None
                self.run.update_step(step["id"], outputs=[])
            after = snapshot_workspace(isolated["stage_dir"])
            mutated = after["fingerprint_sha256"] != isolated["before"]["fingerprint_sha256"]
            if mutated and not assessment.writes:
                raise StepExecutionError("shell step modified its isolated workspace without declaring writes")
            if result.timed_out:
                raise StepExecutionError("shell step timed out")
            if result.returncode != 0:
                raise StepExecutionError("shell step returned %d" % result.returncode)
        finally:
            discard_stage(self.run.run_dir, isolated["stage_dir"])
        return {
            "returncode": result.returncode,
            "duration_ms": result.duration_ms,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "output_sha256": record["sha256"] if record else None,
            "isolated_workspace_mutated": mutated,
        }

    def _codex(self, step: Dict) -> Dict:
        enforce_agent(step, self.policy)
        workspace = self._provider_workspace(step)
        prompt = self._load_prompt(step)
        prompt += self._dependency_context(step)
        if self.iteration_context:
            feedback = self.iteration_context.replace(
                ITERATION_CONTEXT_BEGIN, "[escaped-feedback-begin]"
            ).replace(ITERATION_CONTEXT_END, "[escaped-feedback-end]")
            prompt += "\n\n%s\n%s\n%s\n" % (
                ITERATION_CONTEXT_BEGIN,
                feedback,
                ITERATION_CONTEXT_END,
            )
        output_relative = step.get("capture", "%s.md" % step["id"])
        schema_relative = self._prepare_output_schema(step)
        record = self.run.state["steps"][step["id"]]
        result = invoke_codex(
            run=self.run,
            step=step,
            prompt=prompt,
            workspace=workspace,
            output_relative=output_relative,
            max_tokens=cast(int, step.get("max_tokens", self.workflow.get("agent_max_tokens"))),
            timeout_seconds=step.get(
                "timeout_seconds",
                self.workflow.get("agent_timeout_seconds", self.workflow.get("default_timeout_seconds", 900)),
            ),
            output_limit_bytes=step.get(
                "output_limit_bytes",
                self.workflow.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES),
            ),
            resume_session_id=record.get("resume_session_id"),
            output_schema_relative=schema_relative,
        )
        updates = {
            "outputs": [output_relative],
            "session_id_sha256": sha256_bytes(result.session_id.encode("utf-8")),
            "resume_session_id": None,
            "resume_available": False,
        }
        metrics = {
            "output_sha256": result.output_sha256,
            "duration_ms": result.process.duration_ms,
            "usage": result.usage,
            "receipt": result.receipt_relative,
        }
        if step.get("sandbox", "read-only") == "workspace-write":
            evidence = finalize_stage(
                run_dir=self.run.run_dir,
                run_id=self.run.descriptor["run_id"],
                step_id=step["id"],
                workspace=self.workspace,
                stage_dir=self._stage["stage_dir"],
                before=self._stage["before"],
            )
            relative = "stages/%s.json" % step["id"]
            self.run.write_artifact(relative, canonical_json_bytes(evidence), replace=True)
            updates.update(stage_evidence=relative, pending_stage=None)
            self._stage["evidence_relative"] = relative
            metrics["stage_evidence"] = relative
            metrics["staged_changes"] = evidence["change_count"]
        self.run.update_step(step["id"], **updates)
        return metrics

    def _agent_map(self, step: Dict) -> Dict:
        packets = self._map_packets(step)
        workers = min(step.get("max_workers", self.max_workers), self.max_workers, len(packets))
        enforce_agent(step, self.policy, workers=workers)
        per_call = cast(int, step.get("max_tokens", self.workflow.get("agent_max_tokens")))
        total_cap = cast(int, step.get("max_total_tokens", self.workflow.get("agent_map_max_total_tokens")))
        previous = self.run.state["steps"][step["id"]].get("packets", {})
        packet_records = dict(previous) if isinstance(previous, dict) else {}
        outputs = [None] * len(packets)
        used = 0
        pending = []
        for index, packet in enumerate(packets, start=1):
            key = "%04d" % index
            cached = packet_records.get(key)
            if isinstance(cached, dict) and cached.get("status") == "completed":
                output = cached.get("output")
                self.run.read_artifact(output)
                outputs[index - 1] = output
                charged = cached.get("charged_tokens")
                if not isinstance(charged, int) or charged < 0:
                    raise ValidationError("cached map packet budget is invalid")
                used += charged
            else:
                pending.append((index, key, packet, cached or {}))
        if used > total_cap:
            raise ValidationError("cached map usage exceeds aggregate budget")
        while pending:
            wave = pending[:workers]
            pending = pending[workers:]
            remaining = total_cap - used
            cap = min(per_call, remaining // len(wave))
            if cap < 100:
                raise StepExecutionError("agent map aggregate token budget is exhausted")
            futures = {}
            with ThreadPoolExecutor(max_workers=len(wave)) as executor:
                for index, key, packet, cached in wave:
                    future = executor.submit(self._run_map_packet, step, index, packet, cap, cached)
                    futures[future] = (index, key)
                failure = None
                for future in as_completed(futures):
                    index, key = futures[future]
                    try:
                        packet_record = future.result()
                    except Exception as exc:
                        failure = failure or exc
                        failed = {
                            "status": "failed",
                            "error_class": exc.__class__.__name__,
                        }
                        if isinstance(exc, CodexInvocationError) and exc.resumable:
                            failed["resume_session_id"] = exc.session_id
                        packet_records[key] = failed
                    else:
                        packet_records[key] = packet_record
                        outputs[index - 1] = packet_record["output"]
                        used += packet_record["charged_tokens"]
                    self.run.update_step(step["id"], packets=packet_records)
                if failure is not None:
                    raise failure
        clean_outputs = [value for value in outputs if isinstance(value, str)]
        if len(clean_outputs) != len(packets):
            raise StepExecutionError("agent map did not produce every packet")
        self.run.update_step(step["id"], outputs=clean_outputs, packets=packet_records)
        return {
            "packets": len(packets),
            "workers": workers,
            "charged_tokens": used,
            "max_total_tokens": total_cap,
        }

    def _run_map_packet(self, step: Dict, index: int, packet, cap: int, cached: Dict) -> Dict:
        rendered = _render_item(packet)
        rendered = rendered.replace(MAP_ITEM_BEGIN, "[escaped-map-item-begin]").replace(
            MAP_ITEM_END, "[escaped-map-item-end]"
        )
        rendered = "%s\n%s\n%s" % (MAP_ITEM_BEGIN, rendered, MAP_ITEM_END)
        prompt = step["prompt_template"].format(item=rendered, index=index)
        output = "%s/%04d-%s.md" % (
            step.get("capture_dir", step["id"]),
            index,
            hashlib.sha256(canonical_json_bytes(packet)).hexdigest()[:12],
        )
        packet_step = dict(step)
        packet_step["id"] = step["id"]
        result = invoke_codex(
            run=self.run,
            step=packet_step,
            prompt=prompt,
            workspace=self.workspace,
            output_relative=output,
            max_tokens=cap,
            timeout_seconds=step.get(
                "timeout_seconds",
                self.workflow.get("agent_timeout_seconds", self.workflow.get("default_timeout_seconds", 900)),
            ),
            output_limit_bytes=step.get(
                "output_limit_bytes",
                self.workflow.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES),
            ),
            resume_session_id=cached.get("resume_session_id"),
            invocation_id="%s-p%04d" % (step["id"], index),
            output_schema_relative=self._prepare_output_schema(step, suffix="p%04d" % index),
        )
        total = result.usage.get("total_tokens")
        charged = total if isinstance(total, int) else cap
        return {
            "status": "completed",
            "output": output,
            "output_sha256": result.output_sha256,
            "charged_tokens": charged,
            "cap_tokens": cap,
            "session_id_sha256": sha256_bytes(result.session_id.encode("utf-8")),
            "receipt": result.receipt_relative,
        }

    def _collect_results(self, step: Dict) -> Dict:
        source = self.run.state["steps"].get(step["source_step"], {})
        outputs = source.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise ValidationError("collect_results source has no outputs")
        values = []
        total_input = 0
        for relative in outputs:
            payload = self.run.read_artifact(relative, MAX_CONTEXT_ARTIFACT_BYTES)
            total_input += len(payload)
            if total_input > MAX_COLLECT_INPUT_BYTES:
                raise StepExecutionError("collected inputs exceed %d bytes" % MAX_COLLECT_INPUT_BYTES)
            try:
                value = strict_json_bytes(payload, "map result")
            except ValidationError:
                value = payload.decode("utf-8", errors="replace")
            if step.get("filter_falsey") and not value:
                continue
            values.append(value)
        payload = canonical_json_bytes(values)
        limit = step.get("output_limit_bytes", self.workflow.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES))
        if len(payload) > limit:
            raise StepExecutionError("collected result exceeds output limit")
        record = self.run.write_artifact(step["output"], payload)
        self.run.update_step(step["id"], outputs=[step["output"]], source_outputs=len(outputs))
        return {
            "source_outputs": len(outputs),
            "result_count": len(values),
            "output_sha256": record["sha256"],
        }

    def _load_prompt(self, step: Dict, *, allow_missing_artifact: bool = False) -> str:
        if step.get("prompt") is not None:
            return step["prompt"]
        if step.get("prompt_file") is not None:
            path = resolve_under(self.workspace, step["prompt_file"], "prompt file")
            return read_regular_text(path, "prompt file", 256 * 1024)
        if allow_missing_artifact:
            return ""
        return self.run.read_artifact(step["prompt_artifact"], 256 * 1024).decode("utf-8")

    def _dependency_context(self, step: Dict) -> str:
        source_ids = step.get("context_from", [])
        if not source_ids:
            return ""
        if len(source_ids) > MAX_CONTEXT_SOURCES:
            raise ValidationError("dependency context source count is invalid")
        sections = []
        total = 0
        artifacts = 0
        for source_id in source_ids:
            record = self.run.state["steps"].get(source_id, {})
            outputs = record.get("outputs")
            if not isinstance(outputs, list) or not outputs:
                raise ValidationError("dependency context source has no outputs")
            for relative in outputs:
                artifacts += 1
                if artifacts > MAX_CONTEXT_SOURCES:
                    raise ValidationError("dependency context has too many artifacts")
                payload = self.run.read_artifact(relative, MAX_CONTEXT_ARTIFACT_BYTES)
                excerpt = payload[:MAX_CONTEXT_EXCERPT_BYTES].decode("utf-8", errors="replace")
                excerpt = redact_text(excerpt).replace(CONTEXT_BEGIN, "[escaped-context-begin]").replace(
                    CONTEXT_END, "[escaped-context-end]"
                )
                section = "Source %s artifact %d sha256=%s bytes=%d\n%s" % (
                    source_id,
                    artifacts,
                    sha256_bytes(payload),
                    len(payload),
                    excerpt,
                )
                encoded = section.encode("utf-8")
                if total + len(encoded) > MAX_CONTEXT_BYTES:
                    raise ValidationError("dependency context exceeds %d bytes" % MAX_CONTEXT_BYTES)
                sections.append(section)
                total += len(encoded)
        return "\n\n%s\n%s\n%s\n" % (CONTEXT_BEGIN, "\n\n".join(sections), CONTEXT_END)

    def _map_packets(self, step: Dict) -> list:
        items = self._map_items(step)
        maximum = step.get("max_packets")
        if maximum is None or maximum >= len(items):
            return items
        packets = []
        for index in range(maximum):
            start = index * len(items) // maximum
            end = (index + 1) * len(items) // maximum
            packets.append(items[start:end])
        return packets

    def _map_items(self, step: Dict) -> list:
        if step.get("items") is not None:
            values = list(step["items"])
        elif step.get("items_file") is not None:
            path = resolve_under(self.workspace, step["items_file"], "map items file")
            values = [line.strip() for line in read_regular_text(path, "map items file", 2 * 1024 * 1024).splitlines() if line.strip()]
        else:
            payload = self.run.read_artifact(step["items_artifact"], 2 * 1024 * 1024)
            value = strict_json_bytes(payload, "map items artifact")
            if step.get("items_pointer"):
                value = _json_pointer(value, step["items_pointer"])
            values = value
        maximum = min(MAX_ITEMS, self.workflow.get("max_items", MAX_ITEMS))
        if not isinstance(values, list) or not values or len(values) > maximum:
            raise ValidationError("map item source must contain a bounded non-empty list")
        semantics = step.get("item_semantics", "workspace_path")
        for value in values:
            validate_map_item(value, semantics, step["id"])
        if not step.get("preserve_duplicate_items"):
            seen = set()
            unique = []
            for item in values:
                key = canonical_json_bytes(item)
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            values = unique
        return values

    def _prepare_output_schema(self, step: Dict, suffix: str = "") -> Optional[str]:
        schema = step.get("output_schema")
        if schema is None:
            return None
        name = step["id"] + ("-" + suffix if suffix else "")
        relative = ".schemas/%s.json" % name
        payload = canonical_json_bytes(schema)
        path = self.run.artifact_path(relative)
        if path.exists():
            observed = self.run.read_artifact(relative)
            if observed != payload:
                raise ValidationError("output schema changed during resume")
        else:
            self.run.write_artifact(relative, payload)
        return relative

    def _provider_workspace(self, step: Dict) -> Path:
        if step.get("sandbox", "read-only") != "workspace-write":
            return self._active_workspace(step)
        if self._stage is None:
            stage_dir = resolve_under(
                self.run.run_dir,
                "stages/%s/workspace" % step["id"],
                "stage directory",
            )
            if stage_dir.parent.exists() or stage_dir.parent.is_symlink():
                self.run.update_step(
                    step["id"],
                    pending_stage=None,
                    resume_session_id=None,
                    resume_available=False,
                )
                discard_stage(self.run.run_dir, stage_dir)
            self._stage = create_stage(self.run.run_dir, step["id"], self.workspace)
            self._stage["step_id"] = step["id"]
            attempt = self.run.state["steps"][step["id"]]["attempt"]
            descriptor = pending_stage_descriptor(
                run_dir=self.run.run_dir,
                run_id=self.run.descriptor["run_id"],
                step_id=step["id"],
                attempt=attempt,
                workspace=self.workspace,
                stage_dir=self._stage["stage_dir"],
                before=self._stage["before"],
            )
            relative = "stages/%s.attempt-%d.pending.json" % (step["id"], attempt)
            self.run.write_artifact(relative, canonical_json_bytes(descriptor))
            self.run.update_step(step["id"], pending_stage=relative)
            self._stage["pending_relative"] = relative
        if self._stage.get("step_id") != step["id"]:
            raise ValidationError("writable stage belongs to another step")
        self._validate_active_stage()
        return self._stage["stage_dir"]

    def _validate_active_stage(self) -> None:
        if self._stage is None:
            return
        validate_stage_workspace(self._stage["stage_dir"])
        source = snapshot_workspace(self.workspace)
        if source["fingerprint_sha256"] != self._stage["before"]["fingerprint_sha256"]:
            raise ValidationError("source workspace changed while staged work was running")

    def _handle_failed_stage(self, step: Dict, failure: Exception, resumable: bool):
        if self._stage is None or self._stage.get("step_id") != step["id"]:
            return failure, resumable
        if resumable:
            try:
                self._validate_active_stage()
            except (ConductorError, OSError, ValueError) as exc:
                failure = exc
                resumable = False
        if not resumable:
            try:
                self.run.update_step(
                    step["id"],
                    pending_stage=None,
                    resume_session_id=None,
                    resume_available=False,
                )
                discard_stage(self.run.run_dir, self._stage["stage_dir"])
                self._stage = None
            except (ConductorError, OSError, ValueError) as exc:
                failure = exc
        return failure, resumable

    def _discard_invalid_downstream_stage(self, failure: Exception) -> Exception:
        if self._stage is None:
            return failure
        try:
            validate_stage_workspace(self._stage["stage_dir"])
        except (ConductorError, OSError, ValueError) as exc:
            failure = exc
            try:
                discard_stage(self.run.run_dir, self._stage["stage_dir"])
                self._stage = None
            except (ConductorError, OSError, ValueError) as cleanup_exc:
                failure = cleanup_exc
        return failure

    def _shell_workspace(self, step: Dict, root: Path) -> Path:
        if step.get("cwd"):
            root = resolve_under(root, step["cwd"], "shell cwd")
            if not root.is_dir():
                raise ValidationError("shell cwd must be a directory")
        return root

    def _active_workspace(self, step: Dict) -> Path:
        if self._stage is None:
            return self.workspace
        if self._depends_on(step["id"], self._stage["step_id"]):
            return self._stage["stage_dir"]
        return self.workspace

    def _dependencies_complete(self, step: Dict) -> bool:
        accepted = {"completed", "planned"} if self.dry_run else {"completed"}
        return all(self.run.state["steps"][value]["status"] in accepted for value in step.get("depends_on", []))

    def _depends_on(self, step_id: str, ancestor: str) -> bool:
        by_id = {step["id"]: step for step in self.workflow["steps"]}
        pending = list(by_id[step_id].get("depends_on", []))
        seen = set()
        while pending:
            value = pending.pop()
            if value == ancestor:
                return True
            if value not in seen:
                seen.add(value)
                pending.extend(by_id[value].get("depends_on", []))
        return False

    def _verify_completed_step(self, step: Dict, record: Dict) -> None:
        outputs = record.get("outputs", [])
        if not isinstance(outputs, list):
            raise ValidationError("completed step output index is invalid")
        for output in outputs:
            self.run.read_artifact(output)
        if step["kind"] in {"write_artifact", "collect_results", "codex_exec", "agent_map"} and not outputs:
            raise ValidationError("completed step is missing output evidence")

    def _load_stage_state(self):
        for step in self.workflow["steps"]:
            record = self.run.state["steps"].get(step["id"], {})
            evidence_relative = record.get("stage_evidence")
            if evidence_relative:
                evidence = strict_json_bytes(self.run.read_artifact(evidence_relative), "stage evidence")
                validate_stage_evidence(evidence)
                return {
                    "step_id": step["id"],
                    "stage_dir": resolve_under(self.run.run_dir, evidence["stage_subdir"], "stage directory"),
                    "before": evidence["before"],
                    "evidence_relative": evidence_relative,
                }
        for step in self.workflow["steps"]:
            record = self.run.state["steps"].get(step["id"], {})
            pending_relative = record.get("pending_stage")
            if pending_relative is not None:
                if step.get("sandbox", "read-only") != "workspace-write":
                    raise ValidationError("pending stage belongs to a non-writer step")
                return load_pending_stage(
                    run=self.run,
                    descriptor_relative=pending_relative,
                    step_id=step["id"],
                    workspace=self.workspace,
                )
        return None

    def _verify_and_seal_stage(self) -> None:
        if self._stage is None:
            return
        try:
            validate_stage_workspace(self._stage["stage_dir"])
        except (ConductorError, OSError, ValueError):
            discard_stage(self.run.run_dir, self._stage["stage_dir"])
            self._stage = None
            raise
        source = snapshot_workspace(self.workspace)
        if source["fingerprint_sha256"] != self._stage["before"]["fingerprint_sha256"]:
            raise ValidationError("source workspace changed while staged work was running")
        pending_relative = self._stage.get("evidence_relative")
        if not pending_relative:
            raise ValidationError("staged work is missing its writer evidence")
        pending = strict_json_bytes(self.run.read_artifact(pending_relative), "pending stage evidence")
        validate_stage_evidence(pending)
        observed_stage = snapshot_workspace(self._stage["stage_dir"])
        if observed_stage["fingerprint_sha256"] != pending.get("after", {}).get("fingerprint_sha256"):
            raise ValidationError("a downstream verifier modified the staged workspace")
        verifier_ids = staged_verifier_ids(self.workflow["steps"], self._stage["step_id"])
        completed = [
            step_id
            for step_id in verifier_ids
            if self.run.state["steps"].get(step_id, {}).get("status") == "completed"
        ]
        if not completed:
            raise ValidationError("staged work has no completed downstream verifier")
        evidence = finalize_stage(
            run_dir=self.run.run_dir,
            run_id=self.run.descriptor["run_id"],
            step_id=self._stage["step_id"],
            workspace=self.workspace,
            stage_dir=self._stage["stage_dir"],
            before=self._stage["before"],
        )
        evidence = mark_stage_verified(evidence, completed)
        relative = self._stage.get("evidence_relative") or "stages/%s.json" % self._stage["step_id"]
        self.run.write_artifact(relative, canonical_json_bytes(evidence), replace=True)
        self.run.update_step(self._stage["step_id"], stage_evidence=relative)


def _render_item(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _json_pointer(value, pointer: str):
    current = value
    for raw in pointer.split("/")[1:]:
        part = _decode_json_pointer_token(raw)
        if isinstance(current, list):
            if (
                not part
                or any(char not in "0123456789" for char in part)
                or (len(part) > 1 and part.startswith("0"))
                or int(part) >= len(current)
            ):
                raise ValidationError("map items JSON pointer is missing")
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValidationError("map items JSON pointer is missing")
    return current


def _decode_json_pointer_token(value: str) -> str:
    result = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "~":
            result.append(char)
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValidationError("map items JSON pointer has an invalid escape")
        result.append("~" if value[index + 1] == "0" else "/")
        index += 2
    return "".join(result)
