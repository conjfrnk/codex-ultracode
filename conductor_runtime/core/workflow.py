"""Strict workflow contract for the default runtime."""

import hashlib
import json
import re
import string
from pathlib import Path
from typing import Dict, Iterable, List

from ..errors import ValidationError
from .policy import RISK_LEVELS, normalize_command
from .safe import canonical_json_bytes, read_regular_bytes, require_relative, strict_json_bytes


SCHEMA = "conductor.workflow.v1"
STEP_KINDS = {"write_artifact", "collect_results", "manual_gate", "shell", "codex_exec", "agent_map"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
EFFORTS = {"low", "medium", "high", "xhigh", "ultra"}
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
MAX_STEPS = 128
MAX_WORKERS = 32
MAX_ITEMS = 1000
MAX_CONTEXT_SOURCES = 32
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_PROMPT_CHARS = 256 * 1024
MAX_CONTENT_CHARS = 2 * 1024 * 1024
MAX_TOKEN_CAP = 10**9
MIN_TOKEN_CAP = 100

TOP_LEVEL_FIELDS = {
    "schema",
    "name",
    "description",
    "mode",
    "risk",
    "max_workers",
    "max_items",
    "default_timeout_seconds",
    "agent_timeout_seconds",
    "output_limit_bytes",
    "agent_effort",
    "agent_max_tokens",
    "agent_map_max_total_tokens",
    "result_artifact",
    "steps",
}
INTERNAL_ARTIFACT_ROOTS = {".receipts", ".schemas", "apply-backups", "stages"}
COMMON_STEP_FIELDS = {
    "id",
    "kind",
    "description",
    "risk",
    "depends_on",
    "phase",
    "timeout_seconds",
    "output_limit_bytes",
}
STEP_FIELDS = {
    "write_artifact": COMMON_STEP_FIELDS | {"output", "content"},
    "collect_results": COMMON_STEP_FIELDS
    | {"source_step", "output", "filter_falsey", "intermediate"},
    "manual_gate": COMMON_STEP_FIELDS | {"approval_id"},
    "shell": COMMON_STEP_FIELDS
    | {
        "command",
        "cwd",
        "capture",
        "capture_mode",
        "writes",
        "destructive",
        "network",
    },
    "codex_exec": COMMON_STEP_FIELDS
    | {
        "prompt",
        "prompt_file",
        "prompt_artifact",
        "capture",
        "sandbox",
        "model",
        "effort",
        "max_tokens",
        "context_from",
        "completion_verdict",
        "output_schema",
    },
    "agent_map": COMMON_STEP_FIELDS
    | {
        "items",
        "items_file",
        "items_artifact",
        "items_pointer",
        "item_semantics",
        "preserve_duplicate_items",
        "prompt_template",
        "capture_dir",
        "sandbox",
        "model",
        "effort",
        "max_tokens",
        "max_total_tokens",
        "max_workers",
        "max_packets",
        "output_schema",
    },
}


def slugify(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return clean or "workflow"


def load_workflow(path: Path) -> Dict:
    payload = read_regular_bytes(path, "workflow", MAX_WORKFLOW_BYTES)
    value = strict_json_bytes(payload, "workflow")
    if not isinstance(value, dict):
        raise ValidationError("workflow must contain a JSON object")
    validate_workflow(value, source=str(path))
    value["_source_path"] = str(path)
    return value


def validate_workflow(workflow: Dict, source: str = "<memory>") -> None:
    if not isinstance(workflow, dict):
        raise ValidationError("%s must be an object" % source)
    unknown = sorted(set(workflow) - TOP_LEVEL_FIELDS)
    if unknown:
        raise ValidationError("%s contains unsupported fields: %s" % (source, ", ".join(unknown)))
    if workflow.get("schema") != SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, SCHEMA))
    name = workflow.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValidationError("%s must set a bounded non-empty name" % source)
    _optional_text(workflow, "description", source, 4096)
    mode = workflow.get("mode", "read_only")
    if not isinstance(mode, str) or mode not in {"read_only", "workspace_write", "review", "custom"}:
        raise ValidationError("%s mode is unsupported" % source)
    risk = workflow.get("risk", "low")
    if not isinstance(risk, str) or risk not in RISK_LEVELS:
        raise ValidationError("%s risk is unsupported" % source)
    _optional_int(workflow, "max_workers", source, 1, MAX_WORKERS)
    _optional_int(workflow, "max_items", source, 1, MAX_ITEMS)
    _optional_int(workflow, "default_timeout_seconds", source, 1, MAX_TIMEOUT_SECONDS)
    _optional_int(workflow, "agent_timeout_seconds", source, 1, MAX_TIMEOUT_SECONDS)
    _optional_int(workflow, "output_limit_bytes", source, 1, MAX_OUTPUT_BYTES)
    _optional_effort(workflow, "agent_effort", source)
    _optional_tokens(workflow, "agent_max_tokens", source)
    _optional_tokens(workflow, "agent_map_max_total_tokens", source)
    result_artifact = workflow.get("result_artifact")
    if result_artifact is not None:
        require_relative(result_artifact, "%s result_artifact" % source)

    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ValidationError("%s must contain 1-%d steps" % (source, MAX_STEPS))
    seen = set()
    for step in steps:
        validate_step(step, seen, workflow)
    _validate_dependencies(steps)
    _validate_context(steps)
    _validate_outputs(workflow, steps)
    _validate_map_synthesis(steps)
    _validate_staged_verification(steps)


def validate_step(step: Dict, seen: set, workflow: Dict) -> None:
    if not isinstance(step, dict):
        raise ValidationError("each workflow step must be an object")
    if any(isinstance(key, str) and key.startswith("_") for key in step):
        raise ValidationError("workflow steps cannot set internal fields")
    step_id = step.get("id")
    if not isinstance(step_id, str) or SAFE_ID.fullmatch(step_id) is None:
        raise ValidationError("step id must be a safe bounded identifier")
    if step_id in seen:
        raise ValidationError("duplicate step id %s" % step_id)
    seen.add(step_id)
    kind = step.get("kind")
    if not isinstance(kind, str) or kind not in STEP_KINDS:
        raise ValidationError("step %s has unsupported kind %r" % (step_id, kind))
    unknown = sorted(set(step) - STEP_FIELDS[kind])
    if unknown:
        raise ValidationError("step %s contains unsupported fields: %s" % (step_id, ", ".join(unknown)))
    risk = step.get("risk", "low")
    if not isinstance(risk, str) or risk not in RISK_LEVELS:
        raise ValidationError("step %s has unsupported risk" % step_id)
    _optional_text(step, "description", "step %s" % step_id, 4096)
    _optional_text(step, "phase", "step %s" % step_id, 128, pattern=SAFE_ID)
    _optional_int(step, "timeout_seconds", "step %s" % step_id, 1, MAX_TIMEOUT_SECONDS)
    _optional_int(step, "output_limit_bytes", "step %s" % step_id, 1, MAX_OUTPUT_BYTES)
    dependencies = step.get("depends_on", [])
    if (
        not isinstance(dependencies, list)
        or len(dependencies) > MAX_STEPS
        or not all(isinstance(value, str) and SAFE_ID.fullmatch(value) for value in dependencies)
        or len(dependencies) != len(set(dependencies))
    ):
        raise ValidationError("step %s depends_on must contain unique safe ids" % step_id)

    if kind == "write_artifact":
        require_relative(step.get("output"), "step %s output" % step_id)
        content = step.get("content")
        if not isinstance(content, str) or len(content) > MAX_CONTENT_CHARS:
            raise ValidationError("step %s content must be bounded text" % step_id)
    elif kind == "collect_results":
        source_step = step.get("source_step")
        if not isinstance(source_step, str) or SAFE_ID.fullmatch(source_step) is None:
            raise ValidationError("step %s source_step must be a safe id" % step_id)
        require_relative(step.get("output"), "step %s output" % step_id)
        _optional_bool(step, "filter_falsey", step_id)
        _optional_bool(step, "intermediate", step_id)
    elif kind == "manual_gate":
        approval = step.get("approval_id", step_id)
        if (
            not isinstance(approval, str)
            or not approval
            or len(approval) > 256
            or any(char.isspace() for char in approval)
        ):
            raise ValidationError("step %s approval_id is invalid" % step_id)
    elif kind == "shell":
        normalize_command(step.get("command"))
        if step.get("cwd") is not None:
            require_relative(step["cwd"], "step %s cwd" % step_id)
        if step.get("capture") is not None:
            require_relative(step["capture"], "step %s capture" % step_id)
        capture_mode = step.get("capture_mode", "combined")
        if not isinstance(capture_mode, str) or capture_mode not in {"combined", "stdout", "stderr"}:
            raise ValidationError("step %s capture_mode is unsupported" % step_id)
        for field in ("writes", "destructive", "network"):
            if field not in step:
                raise ValidationError("step %s must explicitly declare %s" % (step_id, field))
            _optional_bool(step, field, step_id)
    elif kind == "codex_exec":
        _validate_codex_step(step, workflow)
    elif kind == "agent_map":
        _validate_map_step(step, workflow)


def _validate_codex_step(step: Dict, workflow: Dict) -> None:
    step_id = step["id"]
    prompts = [step.get("prompt"), step.get("prompt_file"), step.get("prompt_artifact")]
    if sum(value is not None for value in prompts) != 1:
        raise ValidationError("step %s must set exactly one prompt source" % step_id)
    if step.get("prompt") is not None:
        _bounded_text(step["prompt"], "step %s prompt" % step_id, MAX_PROMPT_CHARS)
    for field in ("prompt_file", "prompt_artifact"):
        if step.get(field) is not None:
            require_relative(step[field], "step %s %s" % (step_id, field))
    require_relative(step.get("capture", "%s.md" % step_id), "step %s capture" % step_id)
    sandbox = step.get("sandbox", "read-only")
    if not isinstance(sandbox, str) or sandbox not in {"read-only", "workspace-write"}:
        raise ValidationError("step %s sandbox is unsupported" % step_id)
    if step.get("model") is not None:
        _bounded_text(step["model"], "step %s model" % step_id, 200)
    _optional_effort(step, "effort", "step %s" % step_id)
    _optional_tokens(step, "max_tokens", "step %s" % step_id)
    context = step.get("context_from", [])
    if (
        not isinstance(context, list)
        or len(context) > MAX_CONTEXT_SOURCES
        or not all(isinstance(value, str) and SAFE_ID.fullmatch(value) for value in context)
        or len(context) != len(set(context))
    ):
        raise ValidationError("step %s context_from must contain at most %d unique ids" % (step_id, MAX_CONTEXT_SOURCES))
    verdict = step.get("completion_verdict")
    if verdict is not None and verdict != "strict-v1":
        raise ValidationError("step %s completion_verdict must be strict-v1" % step_id)
    if verdict is not None and step.get("sandbox", "read-only") != "read-only":
        raise ValidationError("step %s completion_verdict requires read-only sandbox" % step_id)
    _validate_output_schema(step.get("output_schema"), "step %s output_schema" % step_id)
    if step.get("max_tokens", workflow.get("agent_max_tokens")) is None:
        raise ValidationError("step %s requires an effective max_tokens budget" % step_id)


def _validate_map_step(step: Dict, workflow: Dict) -> None:
    step_id = step["id"]
    sources = [step.get("items"), step.get("items_file"), step.get("items_artifact")]
    if sum(value is not None for value in sources) != 1:
        raise ValidationError("step %s must set exactly one item source" % step_id)
    semantics = step.get("item_semantics", "workspace_path")
    if not isinstance(semantics, str) or semantics not in {"workspace_path", "opaque", "json"}:
        raise ValidationError("step %s item_semantics is unsupported" % step_id)
    items = step.get("items")
    if items is not None:
        if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
            raise ValidationError("step %s items must contain 1-%d values" % (step_id, MAX_ITEMS))
        for item in items:
            validate_map_item(item, semantics, step_id)
    for field in ("items_file", "items_artifact"):
        if step.get(field) is not None:
            require_relative(step[field], "step %s %s" % (step_id, field))
    pointer = step.get("items_pointer")
    if pointer is not None:
        if step.get("items_artifact") is None or not isinstance(pointer, str) or not pointer.startswith("/"):
            raise ValidationError("step %s items_pointer requires an artifact and JSON pointer" % step_id)
        if len(pointer) > 1024 or any(part in {".", ".."} for part in pointer.split("/")):
            raise ValidationError("step %s items_pointer is invalid" % step_id)
    _optional_bool(step, "preserve_duplicate_items", step_id)
    template = step.get("prompt_template")
    _bounded_text(template, "step %s prompt_template" % step_id, MAX_PROMPT_CHARS)
    fields = []
    try:
        for _, name, format_spec, conversion in string.Formatter().parse(template):
            if name is not None:
                fields.append(name)
                if format_spec or conversion:
                    raise ValidationError(
                        "step %s prompt_template does not support conversions or format specs" % step_id
                    )
    except ValueError as exc:
        raise ValidationError("step %s prompt_template is invalid" % step_id) from exc
    if "item" not in fields or any(field not in {"item", "index"} for field in fields):
        raise ValidationError("step %s prompt_template supports only item and index" % step_id)
    require_relative(step.get("capture_dir", step_id), "step %s capture_dir" % step_id)
    sandbox = step.get("sandbox", "read-only")
    if not isinstance(sandbox, str) or sandbox not in {"read-only", "workspace-write"}:
        raise ValidationError("step %s sandbox is unsupported" % step_id)
    if sandbox != "read-only":
        raise ValidationError("step %s map workers must be read-only" % step_id)
    if step.get("model") is not None:
        _bounded_text(step["model"], "step %s model" % step_id, 200)
    _optional_effort(step, "effort", "step %s" % step_id)
    _optional_tokens(step, "max_tokens", "step %s" % step_id)
    _optional_tokens(step, "max_total_tokens", "step %s" % step_id)
    _optional_int(step, "max_workers", "step %s" % step_id, 1, MAX_WORKERS)
    _optional_int(step, "max_packets", "step %s" % step_id, 1, MAX_ITEMS)
    _validate_output_schema(step.get("output_schema"), "step %s output_schema" % step_id)
    if step.get("max_tokens", workflow.get("agent_max_tokens")) is None:
        raise ValidationError("step %s requires an effective max_tokens budget" % step_id)
    if step.get("max_total_tokens", workflow.get("agent_map_max_total_tokens")) is None:
        raise ValidationError("step %s requires an effective max_total_tokens budget" % step_id)


def _validate_dependencies(steps: List[Dict]) -> None:
    by_id = {step["id"]: step for step in steps}
    order = {step["id"]: index for index, step in enumerate(steps)}
    for step in steps:
        for dependency in step.get("depends_on", []):
            if dependency not in by_id:
                raise ValidationError("step %s depends on unknown step %s" % (step["id"], dependency))
            if order[dependency] >= order[step["id"]]:
                raise ValidationError("step %s dependencies must precede it" % step["id"])
        if step["kind"] == "collect_results":
            source = step["source_step"]
            if source not in step.get("depends_on", []):
                raise ValidationError("collect_results step %s must directly depend on its source" % step["id"])
            if by_id[source]["kind"] != "agent_map":
                raise ValidationError("collect_results step %s source must be agent_map" % step["id"])


def _validate_context(steps: List[Dict]) -> None:
    by_id = {step["id"]: step for step in steps}
    producers = {"write_artifact", "collect_results", "shell", "codex_exec"}
    for step in steps:
        for source in step.get("context_from", []):
            if source not in step.get("depends_on", []):
                raise ValidationError("step %s context source %s must be a direct dependency" % (step["id"], source))
            if source not in by_id or by_id[source]["kind"] not in producers:
                raise ValidationError("step %s context source %s cannot produce evidence" % (step["id"], source))


def _validate_outputs(workflow: Dict, steps: List[Dict]) -> None:
    outputs = []
    directories = []
    for step in steps:
        output = step.get("output") or step.get("capture")
        if step["kind"] == "codex_exec" and output is None:
            output = "%s.md" % step["id"]
        if isinstance(output, str):
            _validate_public_artifact_path(output, "step %s output" % step["id"])
            outputs.append(output)
        if step["kind"] == "agent_map":
            directory = step.get("capture_dir", step["id"])
            _validate_public_artifact_path(directory, "step %s capture_dir" % step["id"])
            directories.append(directory)
    if len(outputs) != len(set(outputs)):
        raise ValidationError("workflow output paths must be unique")
    if len(directories) != len(set(directories)):
        raise ValidationError("workflow map capture directories must be unique")
    paths = [(path, False) for path in outputs] + [(path, True) for path in directories]
    for index, (left, left_is_directory) in enumerate(paths):
        left_parts = Path(left).parts
        for right, right_is_directory in paths[index + 1 :]:
            right_parts = Path(right).parts
            if left_parts == right_parts:
                raise ValidationError("workflow artifact paths must not overlap")
            if left_is_directory and right_parts[: len(left_parts)] == left_parts:
                raise ValidationError("workflow artifact paths must not overlap")
            if right_is_directory and left_parts[: len(right_parts)] == right_parts:
                raise ValidationError("workflow artifact paths must not overlap")
            if not left_is_directory and right_parts[: len(left_parts)] == left_parts:
                raise ValidationError("workflow artifact file cannot contain another output")
            if not right_is_directory and left_parts[: len(right_parts)] == right_parts:
                raise ValidationError("workflow artifact file cannot contain another output")
    result = workflow.get("result_artifact")
    if result is not None and result not in outputs:
        raise ValidationError("result_artifact must name a declared step output")


def _validate_staged_verification(steps: List[Dict]) -> None:
    writers = [
        step
        for step in steps
        if step["kind"] == "codex_exec" and step.get("sandbox", "read-only") == "workspace-write"
    ]
    if len(writers) > 1:
        raise ValidationError("the default runtime supports one staged workspace-write step per workflow")
    if not writers:
        return
    writer = writers[0]
    if not staged_verifier_ids(steps, writer["id"]):
        raise ValidationError(
            "workspace-write step %s requires a downstream shell or strict Codex verifier" % writer["id"]
        )


def _validate_map_synthesis(steps: List[Dict]) -> None:
    for mapped in (step for step in steps if step["kind"] == "agent_map"):
        collectors = [
            step
            for step in steps
            if step["kind"] == "collect_results"
            and step.get("source_step") == mapped["id"]
            and mapped["id"] in step.get("depends_on", [])
        ]
        if len(collectors) != 1:
            raise ValidationError("agent_map step %s requires exactly one result collector" % mapped["id"])
        collector = collectors[0]
        synthesis = [
            step
            for step in steps
            if step["kind"] == "codex_exec"
            and collector["id"] in step.get("depends_on", [])
            and collector["id"] in step.get("context_from", [])
        ]
        if len(synthesis) != 1:
            raise ValidationError(
                "agent_map step %s requires exactly one bounded Codex synthesis step" % mapped["id"]
            )


def staged_verifier_ids(steps: List[Dict], writer_id: str) -> List[str]:
    by_id = {step["id"]: step for step in steps}

    def descends_from(step: Dict) -> bool:
        pending = list(step.get("depends_on", []))
        seen = set()
        while pending:
            value = pending.pop()
            if value == writer_id:
                return True
            if value not in seen:
                seen.add(value)
                pending.extend(by_id[value].get("depends_on", []))
        return False

    return [
        step["id"]
        for step in steps
        if descends_from(step)
        and (
            (step["kind"] == "shell" and not step.get("writes", False))
            or (
                step["kind"] == "codex_exec"
                and step.get("sandbox", "read-only") == "read-only"
                and step.get("completion_verdict") == "strict-v1"
            )
        )
    ]


def workflow_fingerprint(workflow: Dict) -> str:
    public = {key: value for key, value in workflow.items() if not key.startswith("_")}
    validate_workflow(public)
    return hashlib.sha256(canonical_json_bytes(public)).hexdigest()


def workflow_summary(workflow: Dict) -> str:
    validate_workflow({key: value for key, value in workflow.items() if not key.startswith("_")})
    counts = {}
    for step in workflow["steps"]:
        counts[step["kind"]] = counts.get(step["kind"], 0) + 1
    kinds = ", ".join("%s=%d" % item for item in sorted(counts.items()))
    return "%s: %d steps (%s)" % (workflow["name"], len(workflow["steps"]), kinds)


def iter_workflow_files(paths: Iterable[Path]) -> List[Path]:
    files = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(item for item in path.glob("*.json") if item.is_file()))
        elif path.is_file():
            files.append(path)
    return sorted(set(files))


def validate_map_item(value, semantics: str, step_id: str) -> None:
    if semantics in {"workspace_path", "opaque"}:
        if not isinstance(value, str) or not value or len(value) > 65536:
            raise ValidationError("step %s contains an invalid string item" % step_id)
        if semantics == "workspace_path":
            require_relative(value, "step %s item" % step_id)
    else:
        try:
            payload = json.dumps(value, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("step %s contains a non-JSON item" % step_id) from exc
        if len(payload.encode("utf-8")) > 65536:
            raise ValidationError("step %s contains an oversized JSON item" % step_id)


def _validate_output_schema(value, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    try:
        payload = json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("%s must be JSON-compatible" % label) from exc
    if len(payload) > 256 * 1024:
        raise ValidationError("%s exceeds 262144 bytes" % label)


def _optional_bool(value: Dict, field: str, label: str) -> None:
    if field in value and not isinstance(value[field], bool):
        raise ValidationError("%s %s must be boolean" % (label, field))


def _optional_int(value: Dict, field: str, label: str, minimum: int, maximum: int) -> None:
    if field not in value:
        return
    observed = value[field]
    if isinstance(observed, bool) or not isinstance(observed, int) or not minimum <= observed <= maximum:
        raise ValidationError("%s %s must be an integer from %d to %d" % (label, field, minimum, maximum))


def _optional_tokens(value: Dict, field: str, label: str) -> None:
    _optional_int(value, field, label, MIN_TOKEN_CAP, MAX_TOKEN_CAP)


def _optional_effort(value: Dict, field: str, label: str) -> None:
    if field in value and (not isinstance(value[field], str) or value[field] not in EFFORTS):
        raise ValidationError("%s %s is unsupported" % (label, field))


def _optional_text(value: Dict, field: str, label: str, maximum: int, pattern=None) -> None:
    if field not in value:
        return
    observed = value[field]
    if not isinstance(observed, str) or len(observed) > maximum:
        raise ValidationError("%s %s must be bounded text" % (label, field))
    if pattern is not None and pattern.fullmatch(observed) is None:
        raise ValidationError("%s %s is invalid" % (label, field))


def _bounded_text(value, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError("%s must be non-empty bounded text" % label)
    return value


def _validate_public_artifact_path(value: str, label: str) -> None:
    clean = require_relative(value, label)
    if Path(clean).parts[0] in INTERNAL_ARTIFACT_ROOTS:
        raise ValidationError("%s uses a reserved runtime path" % label)
