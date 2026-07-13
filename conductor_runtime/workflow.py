import json
import os
import re
import hashlib
import stat
import string
from pathlib import Path
from typing import Dict, Iterable, List

from .agent_profiles import (
    AGENT_PROFILE_STEP_KINDS,
    AGENT_STEP_KINDS,
    agent_profile_map,
    effective_agent_step,
    validate_agent_profile_context_bindings,
    validate_agent_profile_memory_bindings,
    validate_agent_profile_skill_bindings,
)
from .agent_lifecycle_hooks import (
    AGENT_LIFECYCLE_HOOK_EVENTS,
    AGENT_LIFECYCLE_SCOPES,
)
from .agent_native_tool_hooks import (
    AGENT_NATIVE_TOOL_HOOK_EVENTS,
    MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS,
    validate_agent_native_tool_names,
)
from .agent_lifecycle_context import (
    AGENT_LIFECYCLE_CONTEXT_SCOPES,
    MAX_AGENT_LIFECYCLE_CONTEXT_BYTES,
)
from .agent_memory import MAX_AGENT_MEMORY_ID_CHARS, MAX_AGENT_MEMORY_TAGS, MAX_AGENT_MEMORY_TAG_CHARS
from .agent_team import validate_agent_team_step
from .agent_team_hooks import AGENT_TEAM_HOOK_EVENTS
from .agent_team_quality_retry import MAX_AGENT_TEAM_QUALITY_RETRIES
from .agent_packets import MAX_AGENT_PACKETS, packetize_agent_items
from .codex_config import (
    codex_native_parent_completion_checkpoint,
    codex_native_parent_integration_checkpoint,
    validate_codex_effort,
    validate_codex_native_agents,
    validate_codex_token_cap,
)
from .errors import ValidationError
from .model_verdict import COMPLETION_VERDICT_CONTRACT
from .packet_items import JSON_ITEM_FIELD, clean_packet_items, validate_json_pointer
from .redaction import redact_text
from .security import (
    RISK_LEVELS,
    assess_command,
    normalize_command,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    require_no_path_escape,
)


SCHEMA = "conductor.workflow.v1"
STEP_KINDS = {
    "write_artifact",
    "collect_results",
    "manual_gate",
    "shell",
    "codex_exec",
    "agent_map",
    "agent_team",
    "agent_memory",
}
STANDARD_HOOK_EVENTS = {"before_run", "after_run", "before_step", "after_step"}
HOOK_EVENTS = (
    STANDARD_HOOK_EVENTS
    | AGENT_TEAM_HOOK_EVENTS
    | AGENT_LIFECYCLE_HOOK_EVENTS
    | AGENT_NATIVE_TOOL_HOOK_EVENTS
)
STEP_FILTER_HOOK_EVENTS = (
    {"before_step", "after_step"}
    | AGENT_TEAM_HOOK_EVENTS
    | AGENT_LIFECYCLE_HOOK_EVENTS
    | AGENT_NATIVE_TOOL_HOOK_EVENTS
)
HOOK_FAILURE_MODES = {"block", "warn", "retry"}
AGENT_PROVIDER_STEP_KINDS = {"codex_exec", "agent_map", "agent_team"}
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
MAX_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024
MAX_AGENT_ITEMS = 10000
MAX_AGENT_WORKERS = 128
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_WORKFLOW_JSON_BYTES = 2 * 1024 * 1024
MAX_CODEX_CONTEXT_SOURCES = 32
MAX_CODEX_CONTEXT_ARTIFACTS = 32
MAX_CODEX_CONTEXT_SOURCE_ID_CHARS = 128
MAX_CODEX_OUTPUT_SCHEMA_BYTES = 256 * 1024
CODEX_CONTEXT_SOURCE_KINDS = {"write_artifact", "shell", "codex_exec", "agent_map"}


def slugify(value: str) -> str:
    lowered = value.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "workflow"


def load_workflow(path: Path) -> Dict:
    try:
        data = json.loads(read_regular_text_file_no_follow(path, "workflow", MAX_WORKFLOW_JSON_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ValidationError("%s must contain a JSON object" % path)
    validate_workflow(data, source=str(path))
    data["_source_path"] = str(path)
    return data


def validate_workflow(workflow: Dict, source: str = "<memory>") -> None:
    if workflow.get("schema") != SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, SCHEMA))
    name = workflow.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("%s must set a non-empty string name" % source)
    mode = workflow.get("mode", "read_only")
    if mode not in {"read_only", "workspace_write", "review", "custom"}:
        raise ValidationError("%s has invalid mode %r" % (source, mode))
    max_workers = workflow.get("max_workers", 1)
    if not _is_strict_int(max_workers) or max_workers < 1 or max_workers > MAX_AGENT_WORKERS:
        raise ValidationError("%s max_workers must be an integer from 1 to %d" % (source, MAX_AGENT_WORKERS))
    _validate_optional_output_limit(workflow, "output_limit_bytes", "%s output_limit_bytes" % source)
    _validate_optional_positive_int(workflow, "max_items", "%s max_items" % source, MAX_AGENT_ITEMS)
    _validate_optional_positive_int(
        workflow,
        "default_timeout_seconds",
        "%s default_timeout_seconds" % source,
        MAX_TIMEOUT_SECONDS,
    )
    _validate_optional_positive_int(
        workflow,
        "agent_timeout_seconds",
        "%s agent_timeout_seconds" % source,
        MAX_TIMEOUT_SECONDS,
    )
    _validate_optional_codex_effort(workflow, "agent_effort", "%s agent_effort" % source)
    _validate_optional_codex_token_cap(workflow, "agent_max_tokens", "%s agent_max_tokens" % source)
    _validate_optional_codex_token_cap(
        workflow,
        "agent_map_max_total_tokens",
        "%s agent_map_max_total_tokens" % source,
    )
    agent_profile_map(workflow)
    validate_agent_profile_context_bindings(workflow)
    validate_agent_profile_skill_bindings(workflow)
    validate_agent_profile_memory_bindings(workflow)
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValidationError("%s must contain one or more steps" % source)

    seen = set()
    for step in steps:
        validate_step(step, seen, workflow=workflow)
    _validate_dependencies(steps)
    _validate_result_contract(workflow, steps)
    _validate_codex_context_sources(steps)
    _validate_agent_memory_steps(steps)
    _validate_hooks(workflow.get("hooks"), steps, source)


def validate_step(step: Dict, seen: set, workflow: Dict = None) -> None:
    if not isinstance(step, dict):
        raise ValidationError("each step must be an object")
    reserved = sorted(key for key in step if isinstance(key, str) and key.startswith("_"))
    if reserved:
        raise ValidationError("workflow steps cannot set reserved internal field(s): %s" % ", ".join(reserved))
    misplaced_policy = sorted(
        set(step)
        & {
            "tool_policy",
            "tool_output_token_limit",
            "command_allowlist",
            "allow_apply_patch",
            "skill_scripts",
            "skill_mcp_dependencies",
        }
    )
    if misplaced_policy:
        raise ValidationError(
            "step tool policy must be defined by an agent_profile, not field(s): %s"
            % ", ".join(misplaced_policy)
        )
    step_id = step.get("id")
    if not isinstance(step_id, str) or not SAFE_ID.match(step_id):
        raise ValidationError("step id must be a safe non-empty identifier")
    if step_id in seen:
        raise ValidationError("duplicate step id %s" % step_id)
    seen.add(step_id)

    kind = step.get("kind")
    if kind not in STEP_KINDS:
        raise ValidationError("step %s has unsupported kind %r" % (step_id, kind))
    risk = step.get("risk", "low")
    if risk not in RISK_LEVELS:
        raise ValidationError("step %s has invalid risk %r" % (step_id, risk))
    if "agent_profile" in step and kind not in AGENT_PROFILE_STEP_KINDS:
        raise ValidationError("step %s cannot use an agent_profile" % step_id)
    if "native_agents" in step and kind != "codex_exec":
        raise ValidationError("step %s can use native_agents only with codex_exec" % step_id)
    if "context_from" in step and kind != "codex_exec":
        raise ValidationError("step %s can use context_from only with codex_exec" % step_id)
    if "completion_verdict" in step and kind != "codex_exec":
        raise ValidationError(
            "step %s can use completion_verdict only with codex_exec" % step_id
        )
    if "output_schema" in step and kind not in {"codex_exec", "agent_map"}:
        raise ValidationError("step %s can use output_schema only with codex_exec or agent_map" % step_id)

    agent_step = step
    if kind in AGENT_STEP_KINDS and "agent_profile" in step:
        if workflow is None:
            raise ValidationError("step %s agent_profile requires workflow context" % step_id)
        agent_step = effective_agent_step(workflow, step)

    if "depends_on" in step:
        depends_on = step["depends_on"]
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            raise ValidationError("step %s depends_on must be a list of step ids" % step_id)
    phase = step.get("phase")
    if phase is not None and (not isinstance(phase, str) or not SAFE_ID.match(phase)):
        raise ValidationError("step %s phase must be a safe non-empty identifier" % step_id)
    _validate_optional_output_limit(step, "output_limit_bytes", "step %s output_limit_bytes" % step_id)
    _validate_optional_positive_int(step, "max_items", "step %s max_items" % step_id, MAX_AGENT_ITEMS)
    _validate_optional_positive_int(
        agent_step,
        "timeout_seconds",
        "step %s timeout_seconds" % step_id,
        MAX_TIMEOUT_SECONDS,
    )

    if kind == "write_artifact":
        output = step.get("output")
        content = step.get("content")
        if not isinstance(output, str) or not output:
            raise ValidationError("write_artifact step %s must set output" % step_id)
        require_no_path_escape(output)
        if not isinstance(content, str):
            raise ValidationError("write_artifact step %s must set string content" % step_id)
    elif kind == "collect_results":
        source_step = step.get("source_step")
        if not isinstance(source_step, str) or not SAFE_ID.fullmatch(source_step):
            raise ValidationError(
                "collect_results step %s must set a safe source_step" % step_id
            )
        output = step.get("output")
        if not isinstance(output, str) or not output:
            raise ValidationError("collect_results step %s must set output" % step_id)
        require_no_path_escape(output)
        if not isinstance(step.get("filter_falsey", False), bool):
            raise ValidationError(
                "collect_results step %s filter_falsey must be boolean" % step_id
            )
    elif kind == "manual_gate":
        approval_id = step.get("approval_id", step_id)
        if not isinstance(approval_id, str) or not approval_id:
            raise ValidationError("manual_gate step %s must set approval_id" % step_id)
    elif kind == "shell":
        normalize_command(step.get("command"))
        cwd = step.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd:
                raise ValidationError("shell step %s cwd must be a relative path" % step_id)
            require_no_path_escape(cwd)
        capture = step.get("capture")
        if capture is not None:
            if not isinstance(capture, str) or not capture:
                raise ValidationError("shell step %s capture must be a relative path" % step_id)
            require_no_path_escape(capture)
        capture_mode = step.get("capture_mode", "combined")
        if capture_mode not in {"combined", "stdout", "stderr"}:
            raise ValidationError("shell step %s capture_mode must be combined, stdout, or stderr" % step_id)
    elif kind == "codex_exec":
        _validate_codex_output_schema(step.get("output_schema"), "codex_exec step %s output_schema" % step_id)
        _validate_optional_codex_effort(agent_step, "effort", "codex_exec step %s effort" % step_id)
        _validate_optional_codex_token_cap(agent_step, "max_tokens", "codex_exec step %s max_tokens" % step_id)
        _validate_codex_context_from(step)
        if (
            "completion_verdict" in step
            and step["completion_verdict"] != COMPLETION_VERDICT_CONTRACT
        ):
            raise ValidationError(
                "codex_exec step %s completion_verdict must be %s"
                % (step_id, COMPLETION_VERDICT_CONTRACT)
            )
        if (
            not isinstance(step.get("prompt"), str)
            and not isinstance(step.get("prompt_file"), str)
            and not isinstance(step.get("prompt_artifact"), str)
        ):
            raise ValidationError("codex_exec step %s must set prompt, prompt_file, or prompt_artifact" % step_id)
        if isinstance(step.get("prompt_artifact"), str):
            require_no_path_escape(step["prompt_artifact"])
        if isinstance(step.get("prompt_file"), str):
            require_no_path_escape(step["prompt_file"])
        sandbox = agent_step.get("sandbox", "read-only")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ValidationError("codex_exec step %s sandbox must be read-only or workspace-write" % step_id)
        if "completion_verdict" in step and sandbox != "read-only":
            raise ValidationError(
                "codex_exec step %s completion_verdict requires a read-only sandbox"
                % step_id
            )
        if "native_agents" in step:
            validate_codex_native_agents(
                step["native_agents"],
                "codex_exec step %s native_agents" % step_id,
            )
            if sandbox != "read-only":
                raise ValidationError(
                    "codex_exec step %s native_agents require a read-only sandbox" % step_id
                )
            if agent_step.get("_agent_tool_policy", "standard") != "standard":
                raise ValidationError(
                    "codex_exec step %s native_agents require the standard tool policy" % step_id
                )
            effective_max_tokens = agent_step.get(
                "max_tokens",
                workflow.get("agent_max_tokens") if isinstance(workflow, dict) else None,
            )
            if effective_max_tokens is None:
                raise ValidationError(
                    "codex_exec step %s native_agents require an effective max_tokens cap" % step_id
                )
        capture = step.get("capture", "%s.md" % step_id)
        if not isinstance(capture, str) or not capture:
            raise ValidationError("codex_exec step %s capture must be a relative path" % step_id)
        require_no_path_escape(capture)
    elif kind == "agent_map":
        _validate_codex_output_schema(step.get("output_schema"), "agent_map step %s output_schema" % step_id)
        _validate_optional_codex_effort(agent_step, "effort", "agent_map step %s effort" % step_id)
        _validate_optional_codex_token_cap(agent_step, "max_tokens", "agent_map step %s max_tokens" % step_id)
        _validate_optional_codex_token_cap(
            step,
            "max_total_tokens",
            "agent_map step %s max_total_tokens" % step_id,
        )
        _validate_optional_positive_int(
            step,
            "max_packets",
            "agent_map step %s max_packets" % step_id,
            MAX_AGENT_PACKETS,
        )
        items = step.get("items")
        items_file = step.get("items_file")
        items_artifact = step.get("items_artifact")
        item_semantics = step.get("item_semantics", "workspace_path")
        if item_semantics not in {"workspace_path", "opaque", "json"}:
            raise ValidationError(
                "agent_map step %s item_semantics must be workspace_path, opaque, or json"
                % step_id
            )
        if item_semantics == "opaque" and items is None:
            raise ValidationError(
                "agent_map step %s opaque item semantics require inline items" % step_id
            )
        if item_semantics == "json":
            if items_file is not None:
                raise ValidationError(
                    "agent_map step %s JSON item semantics do not support line-oriented items_file"
                    % step_id
                )
            if items_artifact is not None and step.get("items_pointer") is None:
                raise ValidationError(
                    "agent_map step %s JSON artifact items require items_pointer"
                    % step_id
                )
            if step.get("max_packets") is not None:
                raise ValidationError(
                    "agent_map step %s JSON item semantics preserve one object per packet and do not support max_packets"
                    % step_id
                )
        preserve_duplicates = step.get("preserve_duplicate_items", False)
        if not isinstance(preserve_duplicates, bool):
            raise ValidationError("agent_map step %s preserve_duplicate_items must be boolean" % step_id)
        item_sources = sum(value is not None for value in [items, items_file, items_artifact])
        if item_sources != 1:
            raise ValidationError("agent_map step %s must set exactly one of items, items_file, or items_artifact" % step_id)
        if items is not None:
            if not isinstance(items, list) or not items:
                raise ValidationError("agent_map step %s items must be a non-empty array" % step_id)
            cleaned_items = clean_packet_items(
                items,
                "agent_map step %s items" % step_id,
                MAX_AGENT_ITEMS,
                preserve_duplicates=preserve_duplicates,
                item_semantics=item_semantics,
            )
            packetize_agent_items(cleaned_items, step.get("max_packets"))
        if items_file is not None:
            if not isinstance(items_file, str) or not items_file:
                raise ValidationError("agent_map step %s items_file must be a relative path" % step_id)
            require_no_path_escape(items_file)
        if items_artifact is not None:
            if not isinstance(items_artifact, str) or not items_artifact:
                raise ValidationError("agent_map step %s items_artifact must be a relative path" % step_id)
            require_no_path_escape(items_artifact)
        items_pointer = step.get("items_pointer")
        if items_pointer is not None:
            if items_artifact is None:
                raise ValidationError("agent_map step %s items_pointer requires items_artifact" % step_id)
            validate_json_pointer(items_pointer, "agent_map step %s items_pointer" % step_id)
        prompt_template = step.get("prompt_template")
        _validate_agent_map_prompt_template(
            step_id,
            prompt_template,
            item_semantics=item_semantics,
        )
        capture_dir = step.get("capture_dir", step_id)
        if not isinstance(capture_dir, str) or not capture_dir:
            raise ValidationError("agent_map step %s capture_dir must be a relative path" % step_id)
        require_no_path_escape(capture_dir)
        sandbox = agent_step.get("sandbox", "read-only")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ValidationError("agent_map step %s sandbox must be read-only or workspace-write" % step_id)
        _validate_optional_positive_int(
            agent_step,
            "max_workers",
            "agent_map step %s max_workers" % step_id,
            MAX_AGENT_WORKERS,
        )
    elif kind == "agent_team":
        if workflow is None:
            raise ValidationError("agent_team step %s requires workflow context" % step_id)
        validate_agent_team_step(step, workflow)
    elif kind == "agent_memory":
        if workflow is None:
            raise ValidationError("agent_memory step %s requires workflow context" % step_id)
        profile_name = step.get("agent_profile")
        if not isinstance(profile_name, str) or not SAFE_ID.match(profile_name):
            raise ValidationError("agent_memory step %s must set an agent_profile" % step_id)
        profile = agent_profile_map(workflow).get(profile_name)
        if profile is None:
            raise ValidationError("agent_memory step %s references an unknown agent profile" % step_id)
        memory = profile.get("memory")
        if not isinstance(memory, dict) or memory.get("mode") != "read-write":
            raise ValidationError(
                "agent_memory step %s requires a read-write memory profile" % step_id
            )
        if step.get("risk", "low") == "low":
            raise ValidationError("agent_memory step %s must be medium or high risk" % step_id)
        if step.get("action", "append") != "append":
            raise ValidationError("agent_memory step %s only supports append" % step_id)
        source_artifact = step.get("source_artifact")
        if not isinstance(source_artifact, str) or not source_artifact:
            raise ValidationError("agent_memory step %s must set source_artifact" % step_id)
        require_no_path_escape(source_artifact)
        entry_id = step.get("entry_id")
        if entry_id is not None and (
            not isinstance(entry_id, str)
            or len(entry_id) > MAX_AGENT_MEMORY_ID_CHARS
            or not SAFE_ID.match(entry_id)
        ):
            raise ValidationError("agent_memory step %s entry_id is invalid" % step_id)
        tags = step.get("tags", [])
        if not isinstance(tags, list) or len(tags) > MAX_AGENT_MEMORY_TAGS:
            raise ValidationError(
                "agent_memory step %s tags must contain at most %d values"
                % (step_id, MAX_AGENT_MEMORY_TAGS)
            )
        for tag in tags:
            if (
                not isinstance(tag, str)
                or len(tag) > MAX_AGENT_MEMORY_TAG_CHARS
                or not SAFE_ID.match(tag)
            ):
                raise ValidationError("agent_memory step %s contains an invalid tag" % step_id)
        if len(tags) != len(set(tags)):
            raise ValidationError("agent_memory step %s tags must be unique" % step_id)


def _validate_output_limit(value, label: str) -> None:
    if not _is_strict_int(value) or value < 1 or value > MAX_OUTPUT_LIMIT_BYTES:
        raise ValidationError("%s must be an integer from 1 to %d" % (label, MAX_OUTPUT_LIMIT_BYTES))


def _validate_positive_int(value, label: str, maximum: int) -> None:
    if not _is_strict_int(value) or value < 1 or value > maximum:
        raise ValidationError("%s must be an integer from 1 to %d" % (label, maximum))


def _validate_optional_output_limit(values: Dict, key: str, label: str) -> None:
    if key in values:
        _validate_output_limit(values[key], label)


def _validate_optional_positive_int(values: Dict, key: str, label: str, maximum: int) -> None:
    if key in values:
        _validate_positive_int(values[key], label, maximum)


def _validate_optional_codex_effort(values: Dict, key: str, label: str) -> None:
    if key in values:
        validate_codex_effort(values[key], label)


def _validate_optional_codex_token_cap(values: Dict, key: str, label: str) -> None:
    if key in values:
        validate_codex_token_cap(values[key], label)


def _validate_codex_output_schema(value, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValidationError("%s must be a JSON-compatible object" % label)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be JSON-compatible: %s" % (label, exc.__class__.__name__))
    if len(encoded) > MAX_CODEX_OUTPUT_SCHEMA_BYTES:
        raise ValidationError("%s must be at most %d bytes" % (label, MAX_CODEX_OUTPUT_SCHEMA_BYTES))


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_agent_map_prompt_template(
    step_id: str,
    prompt_template,
    item_semantics: str = "workspace_path",
) -> None:
    if not isinstance(prompt_template, str):
        raise ValidationError("agent_map step %s prompt_template must be a string" % step_id)
    try:
        fields = _format_fields(prompt_template)
    except ValueError as exc:
        raise ValidationError("agent_map step %s prompt_template is invalid: %s" % (step_id, exc.__class__.__name__))
    item_fields = [
        field
        for field in fields
        if field == "item"
        or (item_semantics == "json" and JSON_ITEM_FIELD.fullmatch(field))
    ]
    if not item_fields:
        required = "{item} or a dotted {item.property}" if item_semantics == "json" else "{item}"
        raise ValidationError(
            "agent_map step %s prompt_template must contain %s"
            % (step_id, required)
        )
    supported = {"index", *item_fields}
    unsupported = sorted(set(fields) - supported)
    if unsupported:
        detail = (
            "{item}, dotted {item.property}, and {index}"
            if item_semantics == "json"
            else "{item} and {index}"
        )
        raise ValidationError(
            "agent_map step %s prompt_template may only use %s"
            % (step_id, detail)
        )


def _format_fields(template: str) -> List[str]:
    fields = []
    for _, field_name, format_spec, conversion in string.Formatter().parse(template):
        if field_name is not None:
            if conversion is not None or format_spec:
                raise ValueError("format conversions and specs are not supported")
            fields.append(field_name)
    return fields


def _validate_dependencies(steps: List[Dict]) -> None:
    positions = {step["id"]: index for index, step in enumerate(steps)}
    for index, step in enumerate(steps):
        for dependency in step.get("depends_on", []):
            if dependency not in positions:
                raise ValidationError("step %s depends on unknown step %s" % (step["id"], dependency))
            if dependency == step["id"]:
                raise ValidationError("step %s cannot depend on itself" % step["id"])
            if positions[dependency] >= index:
                raise ValidationError(
                    "step %s depends on %s, but dependencies must appear earlier in the workflow"
                    % (step["id"], dependency)
                )


def _validate_result_contract(workflow: Dict, steps: List[Dict]) -> None:
    result_artifact = workflow.get("result_artifact")
    collectors = [step for step in steps if step["kind"] == "collect_results"]
    if result_artifact is None:
        if collectors:
            raise ValidationError(
                "collect_results requires a workflow result_artifact contract"
            )
        return
    if not isinstance(result_artifact, str) or not result_artifact:
        raise ValidationError("workflow result_artifact must be a relative path")
    require_no_path_escape(result_artifact)
    if len(collectors) != 1:
        raise ValidationError(
            "workflow result_artifact requires exactly one collect_results step"
        )
    collector = collectors[0]
    if collector is not steps[-1]:
        raise ValidationError("collect_results must be the final workflow step")
    if collector["output"] != result_artifact:
        raise ValidationError(
            "collect_results output must equal workflow result_artifact"
        )

    source_id = collector["source_step"]
    step_map = {step["id"]: step for step in steps}
    source = step_map.get(source_id)
    if source is None:
        raise ValidationError(
            "collect_results step %s references unknown source_step %s"
            % (collector["id"], source_id)
        )
    if source_id not in collector.get("depends_on", []):
        raise ValidationError(
            "collect_results step %s source_step must be a direct dependency"
            % collector["id"]
        )
    if source["kind"] not in {"codex_exec", "agent_map"}:
        raise ValidationError(
            "collect_results step %s source_step must be codex_exec or agent_map"
            % collector["id"]
        )
    if collector.get("filter_falsey", False) and source["kind"] != "agent_map":
        raise ValidationError(
            "collect_results step %s filter_falsey requires an agent_map source"
            % collector["id"]
        )

    output_path = Path(collector["output"])
    if source["kind"] == "codex_exec":
        source_path = Path(source.get("capture", "%s.md" % source_id))
        if output_path == source_path:
            raise ValidationError(
                "collect_results output must not overwrite its codex_exec source"
            )
    else:
        source_dir = Path(source.get("capture_dir", source_id))
        if output_path == source_dir or source_dir in output_path.parents:
            raise ValidationError(
                "collect_results output must be outside its agent_map capture_dir"
            )


def _validate_codex_context_from(step: Dict) -> None:
    if "context_from" not in step:
        return
    sources = step["context_from"]
    if (
        not isinstance(sources, list)
        or not sources
        or len(sources) > MAX_CODEX_CONTEXT_SOURCES
    ):
        raise ValidationError(
            "codex_exec step %s context_from must contain 1 to %d step ids"
            % (step["id"], MAX_CODEX_CONTEXT_SOURCES)
        )
    for source_id in sources:
        if (
            not isinstance(source_id, str)
            or len(source_id) > MAX_CODEX_CONTEXT_SOURCE_ID_CHARS
            or not SAFE_ID.match(source_id)
        ):
            raise ValidationError(
                "codex_exec step %s context_from must contain safe step ids of at most %d characters"
                % (step["id"], MAX_CODEX_CONTEXT_SOURCE_ID_CHARS)
            )
    if len(set(sources)) != len(sources):
        raise ValidationError("codex_exec step %s context_from must contain unique step ids" % step["id"])


def _validate_codex_context_sources(steps: List[Dict]) -> None:
    step_map = {step["id"]: step for step in steps}
    for step in steps:
        artifact_upper_bound = 0
        for source_id in step.get("context_from", []):
            if source_id not in step.get("depends_on", []):
                raise ValidationError(
                    "codex_exec step %s context_from source %s must be a direct dependency"
                    % (step["id"], source_id)
                )
            source = step_map[source_id]
            if source["kind"] not in CODEX_CONTEXT_SOURCE_KINDS:
                raise ValidationError(
                    "codex_exec step %s context_from source %s has unsupported kind %s"
                    % (step["id"], source_id, source["kind"])
                )
            if source["kind"] == "shell" and not source.get("capture"):
                raise ValidationError(
                    "codex_exec step %s context_from shell source %s must capture output"
                    % (step["id"], source_id)
                )
            if source["kind"] == "agent_map":
                if isinstance(source.get("items"), list):
                    cleaned = clean_packet_items(
                        source["items"],
                        "agent_map step %s items" % source_id,
                        MAX_AGENT_ITEMS,
                        preserve_duplicates=source.get(
                            "preserve_duplicate_items",
                            False,
                        ),
                        item_semantics=source.get(
                            "item_semantics",
                            "workspace_path",
                        ),
                    )
                    artifact_upper_bound += len(
                        packetize_agent_items(cleaned, source.get("max_packets"))
                    )
                elif (
                    source.get("item_semantics") == "json"
                    and source.get("max_items") is not None
                ):
                    artifact_upper_bound += source["max_items"]
                elif source.get("max_packets") is not None:
                    artifact_upper_bound += source["max_packets"]
                else:
                    raise ValidationError(
                        "codex_exec step %s context_from agent_map source %s must set max_packets or JSON max_items"
                        % (step["id"], source_id)
                    )
            else:
                artifact_upper_bound += 1
        if artifact_upper_bound > MAX_CODEX_CONTEXT_ARTIFACTS:
            raise ValidationError(
                "codex_exec step %s context_from may resolve at most %d artifacts"
                % (step["id"], MAX_CODEX_CONTEXT_ARTIFACTS)
            )


def _validate_agent_memory_steps(steps: List[Dict]) -> None:
    seen_profiles = set()
    for step in steps:
        if step.get("kind") != "agent_memory":
            continue
        profile = step["agent_profile"]
        if profile in seen_profiles:
            raise ValidationError(
                "workflow may contain at most one agent_memory update per profile"
            )
        seen_profiles.add(profile)


def _validate_hooks(raw_hooks, workflow_steps: List[Dict], source: str) -> None:
    if raw_hooks is None:
        return
    if not isinstance(raw_hooks, list):
        raise ValidationError("%s hooks must be an array" % source)
    step_map = {step["id"]: step for step in workflow_steps}
    step_ids = set(step_map)
    seen = set()
    for hook in raw_hooks:
        if not isinstance(hook, dict):
            raise ValidationError("%s hooks must contain objects" % source)
        hook_id = hook.get("id")
        if not isinstance(hook_id, str) or not SAFE_ID.match(hook_id):
            raise ValidationError("%s hook id must be a safe non-empty identifier" % source)
        if hook_id in seen:
            raise ValidationError("%s duplicate hook id %s" % (source, hook_id))
        seen.add(hook_id)
        event = hook.get("event")
        if event not in HOOK_EVENTS:
            raise ValidationError("%s hook %s has unsupported event %r" % (source, hook_id, event))
        normalize_command(hook.get("command"))
        hook_assessment = assess_command(hook.get("command"))
        risk = hook.get("risk", "low")
        if risk not in RISK_LEVELS:
            raise ValidationError("%s hook %s has invalid risk %r" % (source, hook_id, risk))
        cwd = hook.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd:
                raise ValidationError("%s hook %s cwd must be a relative path" % (source, hook_id))
            require_no_path_escape(cwd)
        capture = hook.get("capture")
        if capture is not None:
            if not isinstance(capture, str) or not capture:
                raise ValidationError("%s hook %s capture must be a relative path" % (source, hook_id))
            require_no_path_escape(capture)
        capture_mode = hook.get("capture_mode", "combined")
        if capture_mode not in {"combined", "stdout", "stderr"}:
            raise ValidationError("%s hook %s capture_mode must be combined, stdout, or stderr" % (source, hook_id))
        on_failure = hook.get(
            "on_failure",
            "warn" if event == "agent_stop" else "block",
        )
        if on_failure not in HOOK_FAILURE_MODES:
            raise ValidationError("%s hook %s on_failure must be block, warn, or retry" % (source, hook_id))
        retry_fields = {"max_retries", "retry_exit_codes", "retry_max_tokens"}
        if on_failure == "retry":
            if event not in AGENT_TEAM_HOOK_EVENTS:
                raise ValidationError(
                    "%s hook %s retry mode is only valid for agent-team events"
                    % (source, hook_id)
                )
            max_retries = hook.get("max_retries", 1)
            if (
                not _is_strict_int(max_retries)
                or not 1 <= max_retries <= MAX_AGENT_TEAM_QUALITY_RETRIES
            ):
                raise ValidationError(
                    "%s hook %s max_retries must be an integer from 1 to %d"
                    % (source, hook_id, MAX_AGENT_TEAM_QUALITY_RETRIES)
                )
            retry_exit_codes = hook.get("retry_exit_codes", [2])
            if (
                not isinstance(retry_exit_codes, list)
                or not retry_exit_codes
                or len(retry_exit_codes) > 8
                or not all(
                    _is_strict_int(value) and 1 <= value <= 255
                    for value in retry_exit_codes
                )
                or len(retry_exit_codes) != len(set(retry_exit_codes))
            ):
                raise ValidationError(
                    "%s hook %s retry_exit_codes must contain 1 to 8 unique exit codes"
                    % (source, hook_id)
                )
            _validate_optional_codex_token_cap(
                hook,
                "retry_max_tokens",
                "%s hook %s retry_max_tokens" % (source, hook_id),
            )
        else:
            unexpected_retry_fields = sorted(set(hook) & retry_fields)
            if unexpected_retry_fields:
                raise ValidationError(
                    "%s hook %s retry field(s) require on_failure retry: %s"
                    % (source, hook_id, ", ".join(unexpected_retry_fields))
                )
        for field in ("writes", "destructive", "network", "external_path"):
            if field in hook and not isinstance(hook[field], bool):
                raise ValidationError("%s hook %s %s must be a boolean" % (source, hook_id, field))
        _validate_optional_output_limit(hook, "output_limit_bytes", "%s hook %s output_limit_bytes" % (source, hook_id))
        _validate_optional_positive_int(
            hook,
            "timeout_seconds",
            "%s hook %s timeout_seconds" % (source, hook_id),
            MAX_TIMEOUT_SECONDS,
        )
        steps = hook.get("steps")
        if steps is not None:
            if event not in STEP_FILTER_HOOK_EVENTS:
                raise ValidationError("%s hook %s steps filter is only valid for step events" % (source, hook_id))
            if (
                not isinstance(steps, list)
                or not steps
                or not all(isinstance(item, str) for item in steps)
                or len(steps) != len(set(steps))
            ):
                raise ValidationError("%s hook %s steps must be a non-empty step id array" % (source, hook_id))
            unknown = sorted(set(steps) - step_ids)
            if unknown:
                raise ValidationError("%s hook %s references unknown step(s): %s" % (source, hook_id, ", ".join(unknown)))
        if event in AGENT_LIFECYCLE_HOOK_EVENTS:
            if event == "agent_stop" and on_failure == "retry":
                raise ValidationError(
                    "%s hook %s agent_stop does not support on_failure retry"
                    % (source, hook_id)
                )
            _validate_agent_lifecycle_hook(
                hook,
                hook_assessment,
                workflow_steps,
                steps,
                source,
            )
            continue
        if event in AGENT_NATIVE_TOOL_HOOK_EVENTS:
            _validate_agent_native_tool_hook(
                hook,
                hook_assessment,
                workflow_steps,
                steps,
                source,
            )
            continue
        if "tools" in hook:
            raise ValidationError(
                "%s hook %s tools filter is only valid for agent_pre_tool"
                % (source, hook_id)
            )
        if event not in AGENT_TEAM_HOOK_EVENTS:
            for field in ("members", "tasks", "profiles", "scopes"):
                if field in hook:
                    raise ValidationError(
                        "%s hook %s %s filter is not valid for this event"
                        % (source, hook_id, field)
                    )
            continue

        for field in ("profiles", "scopes"):
            if field in hook:
                raise ValidationError(
                    "%s hook %s %s filter is only valid for agent lifecycle events"
                    % (source, hook_id, field)
                )

        targeted_steps = (
            [step for step in workflow_steps if step.get("kind") == "agent_team"]
            if steps is None
            else [step for step in workflow_steps if step["id"] in steps]
        )
        if not targeted_steps or any(step.get("kind") != "agent_team" for step in targeted_steps):
            raise ValidationError(
                "%s hook %s agent-team events may target only agent_team steps"
                % (source, hook_id)
            )
        for field in ("members", "tasks"):
            if field not in hook:
                continue
            values = hook[field]
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) and SAFE_ID.match(value) for value in values)
                or len(values) != len(set(values))
            ):
                raise ValidationError(
                    "%s hook %s %s must be a non-empty safe id array"
                    % (source, hook_id, field)
                )
        member_filter = hook.get("members")
        if member_filter is not None:
            known_members = {
                member["id"]
                for step in targeted_steps
                for member in step["members"]
            }
            unknown_members = sorted(set(member_filter) - known_members)
            if unknown_members:
                raise ValidationError(
                    "%s hook %s references unknown team member(s): %s"
                    % (source, hook_id, ", ".join(unknown_members))
                )
        if capture is not None or "capture_mode" in hook:
            raise ValidationError(
                "%s hook %s agent-team events cannot use capture; use hook logs and state evidence"
                % (source, hook_id)
            )
        prohibited = [
            field
            for field in ("destructive", "network", "external_path")
            if hook.get(field, False) or getattr(hook_assessment, field)
        ]
        if prohibited:
            raise ValidationError(
                "%s hook %s agent-team events cannot enable %s"
                % (source, hook_id, ", ".join(prohibited))
            )
        if (hook.get("writes", False) or hook_assessment.writes) and any(
            step.get("sandbox", "read-only") != "workspace-write"
            for step in targeted_steps
        ):
            raise ValidationError(
                "%s hook %s write-capable team gates require workspace-write teams"
                % (source, hook_id)
            )


def _validate_agent_lifecycle_hook(
    hook: Dict,
    hook_assessment,
    workflow_steps: List[Dict],
    steps,
    source: str,
) -> None:
    hook_id = hook["id"]
    inject_context = hook.get("inject_context", False)
    if not isinstance(inject_context, bool):
        raise ValidationError(
            "%s hook %s inject_context must be a boolean" % (source, hook_id)
        )
    if "context_limit_bytes" in hook:
        if not inject_context:
            raise ValidationError(
                "%s hook %s context_limit_bytes requires inject_context"
                % (source, hook_id)
            )
        _validate_optional_positive_int(
            hook,
            "context_limit_bytes",
            "%s hook %s context_limit_bytes" % (source, hook_id),
            MAX_AGENT_LIFECYCLE_CONTEXT_BYTES,
        )
    targeted_steps = (
        [step for step in workflow_steps if step.get("kind") in AGENT_PROVIDER_STEP_KINDS]
        if steps is None
        else [step for step in workflow_steps if step["id"] in steps]
    )
    if not targeted_steps or any(
        step.get("kind") not in AGENT_PROVIDER_STEP_KINDS for step in targeted_steps
    ):
        raise ValidationError(
            "%s hook %s agent lifecycle events may target only provider-backed agent steps"
            % (source, hook_id)
        )
    scopes = hook.get("scopes")
    if scopes is not None:
        if (
            not isinstance(scopes, list)
            or not scopes
            or len(scopes) != len(set(scopes))
            or not all(scope in AGENT_LIFECYCLE_SCOPES for scope in scopes)
        ):
            raise ValidationError(
                "%s hook %s scopes must be a non-empty unique agent lifecycle scope array"
                % (source, hook_id)
            )
        supported = {
            "codex_exec": {"codex_exec"},
            "agent_map": {"agent_map_packet"},
            "agent_team": {
                "agent_team_plan",
                "agent_team_review",
                "agent_team_turn",
                "agent_team_quality_retry",
            },
        }
        if not any(set(scopes) & supported[step["kind"]] for step in targeted_steps):
            raise ValidationError(
                "%s hook %s scopes do not match any targeted step"
                % (source, hook_id)
            )
    if inject_context:
        if hook["event"] != "agent_start":
            raise ValidationError(
                "%s hook %s context injection requires agent_start"
                % (source, hook_id)
            )
        if hook.get("on_failure", "block") != "block":
            raise ValidationError(
                "%s hook %s context injection requires on_failure block"
                % (source, hook_id)
            )
        if scopes is None or not set(scopes) <= AGENT_LIFECYCLE_CONTEXT_SCOPES:
            raise ValidationError(
                "%s hook %s context injection requires explicit supported lifecycle scopes"
                % (source, hook_id)
            )
    profiles = hook.get("profiles")
    if profiles is not None:
        if (
            not isinstance(profiles, list)
            or not profiles
            or len(profiles) != len(set(profiles))
            or not all(isinstance(value, str) and SAFE_ID.match(value) for value in profiles)
        ):
            raise ValidationError(
                "%s hook %s profiles must be a non-empty unique safe id array"
                % (source, hook_id)
            )
        workflow_profile_names = {
            step.get("agent_profile")
            for step in workflow_steps
            if isinstance(step.get("agent_profile"), str)
        }
        workflow_profile_names.update(
            member.get("agent_profile")
            for step in workflow_steps
            if step.get("kind") == "agent_team"
            for member in step.get("members", [])
            if isinstance(member.get("agent_profile"), str)
        )
        unknown_profiles = sorted(set(profiles) - workflow_profile_names)
        if unknown_profiles:
            raise ValidationError(
                "%s hook %s references unused or unknown agent profile(s): %s"
                % (source, hook_id, ", ".join(unknown_profiles))
            )
    for field in ("members", "tasks"):
        if field not in hook:
            continue
        values = hook[field]
        if (
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            or not all(isinstance(value, str) and SAFE_ID.match(value) for value in values)
        ):
            raise ValidationError(
                "%s hook %s %s must be a non-empty unique safe id array"
                % (source, hook_id, field)
            )
    team_steps = [step for step in targeted_steps if step.get("kind") == "agent_team"]
    if (hook.get("members") is not None or hook.get("tasks") is not None) and not team_steps:
        raise ValidationError(
            "%s hook %s member/task filters require a targeted agent_team step"
            % (source, hook_id)
        )
    if hook.get("members") is not None:
        known_members = {
            member["id"]
            for step in team_steps
            for member in step["members"]
        }
        unknown_members = sorted(set(hook["members"]) - known_members)
        if unknown_members:
            raise ValidationError(
                "%s hook %s references unknown team member(s): %s"
                % (source, hook_id, ", ".join(unknown_members))
            )
    if hook.get("capture") is not None or "capture_mode" in hook:
        raise ValidationError(
            "%s hook %s agent lifecycle events cannot use capture; use unique hook logs"
            % (source, hook_id)
        )
    prohibited = [
        field
        for field in ("writes", "destructive", "network", "external_path")
        if hook.get(field, False) or getattr(hook_assessment, field)
    ]
    if prohibited:
        raise ValidationError(
            "%s hook %s agent lifecycle events must be local and read-only; prohibited: %s"
            % (source, hook_id, ", ".join(prohibited))
        )


def _validate_agent_native_tool_hook(
    hook: Dict,
    hook_assessment,
    workflow_steps: List[Dict],
    steps,
    source: str,
) -> None:
    hook_id = hook["id"]
    if hook.get("on_failure", "block") not in {"block", "warn"}:
        raise ValidationError(
            "%s hook %s agent_pre_tool supports only block or warn"
            % (source, hook_id)
        )
    validate_agent_native_tool_names(
        hook.get("tools"),
        "%s hook %s tools" % (source, hook_id),
    )
    command = normalize_command(hook["command"])
    if len(command) != 1 or "/" in command[0] or "\\" in command[0]:
        raise ValidationError(
            "%s hook %s agent_pre_tool command must be one executable name on PATH"
            % (source, hook_id)
        )
    prohibited_assessment = any(
        getattr(hook_assessment, field)
        for field in ("writes", "destructive", "network", "external_path")
    )
    if prohibited_assessment and hook.get("risk", "low") != "high":
        raise ValidationError(
            "%s hook %s custom agent_pre_tool verifiers require risk high"
            % (source, hook_id)
        )
    if any(
        hook.get(field, False)
        for field in ("writes", "destructive", "network", "external_path")
    ):
        raise ValidationError(
            "%s hook %s agent_pre_tool cannot declare write, destructive, network, or external authority"
            % (source, hook_id)
        )
    timeout = hook.get("timeout_seconds", 5)
    if timeout > MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS:
        raise ValidationError(
            "%s hook %s agent_pre_tool timeout_seconds must be at most %d"
            % (source, hook_id, MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS)
        )
    for field in (
        "cwd",
        "capture",
        "capture_mode",
        "output_limit_bytes",
        "inject_context",
        "context_limit_bytes",
        "max_retries",
        "retry_exit_codes",
        "retry_max_tokens",
    ):
        if field in hook:
            raise ValidationError(
                "%s hook %s agent_pre_tool does not support %s"
                % (source, hook_id, field)
            )
    _validate_agent_lifecycle_hook(
        hook,
        assess_command(["true"]),
        workflow_steps,
        steps,
        source,
    )


def workflow_summary(workflow: Dict) -> str:
    lines = [
        "name: %s" % redact_text(workflow["name"]),
        "mode: %s" % workflow.get("mode", "read_only"),
        "max_workers: %s" % workflow.get("max_workers", 1),
        "steps:",
    ]
    for step in workflow["steps"]:
        effective = (
            effective_agent_step(workflow, step)
            if step.get("agent_profile") and step.get("kind") in AGENT_STEP_KINDS
            else step
        )
        profile = ""
        if step.get("agent_profile"):
            if step.get("kind") == "agent_memory":
                profile = ", profile=%s, memory=append" % redact_text(step["agent_profile"])
            else:
                profile = ", profile=%s, tools=%s" % (
                    redact_text(step["agent_profile"]),
                    redact_text(effective.get("_agent_tool_policy", "standard")),
                )
        elif step.get("kind") == "agent_team":
            profiles = []
            for member in step.get("members", []):
                name = member.get("agent_profile") if isinstance(member, dict) else None
                if isinstance(name, str) and name not in profiles:
                    profiles.append(name)
            profile = ", team=%d, sandbox=%s, profiles=%s" % (
                len(step.get("members", [])),
                redact_text(step.get("sandbox", "read-only")),
                "+".join(redact_text(name) for name in profiles),
            )
            messaging = step.get("active_messaging")
            if isinstance(messaging, dict):
                profile += ", active-messages=%s, message-depth=%s" % (
                    messaging.get("max_tasks", 0),
                    messaging.get("max_depth", 0),
                )
            operator_chat = step.get("operator_chat")
            if isinstance(operator_chat, dict):
                profile += ", operator-questions=%s, reply-wait=%ss" % (
                    operator_chat.get("max_questions", 0),
                    operator_chat.get("reply_timeout_seconds", 0),
                )
            operator_console = step.get("operator_console")
            if isinstance(operator_console, dict):
                profile += ", operator-console=%s-events/%s-bytes" % (
                    operator_console.get("max_events", 0),
                    operator_console.get("max_bytes", 0),
                )
            plan_approval = step.get("plan_approval")
            if isinstance(plan_approval, dict):
                reviewer = plan_approval.get("reviewer", "lead")
                profile += ", plan-reviewer=%s" % reviewer
                if reviewer == "operator":
                    profile += ", plan-wait=%ss" % plan_approval.get(
                        "reply_timeout_seconds",
                        0,
                    )
        if step.get("kind") == "agent_map" and step.get("max_workers") is not None:
            profile += ", workers=%s" % step["max_workers"]
        if step.get("phase") is not None:
            profile += ", phase=%s" % redact_text(step["phase"])
        if effective.get("effort") is not None:
            profile += ", effort=%s" % redact_text(effective["effort"])
        if step.get("kind") == "agent_map" and step.get("item_semantics") in {"opaque", "json"}:
            profile += ", items=%s" % step["item_semantics"]
        if step.get("native_agents"):
            max_tokens = effective.get("max_tokens", workflow.get("agent_max_tokens"))
            profile += ", native-agents=max-%s/depth-1" % step["native_agents"]["max_threads"]
            if max_tokens is not None:
                profile += ", parent-integration-at-%s/synthesis-at-%s-remaining" % (
                    codex_native_parent_integration_checkpoint(max_tokens),
                    codex_native_parent_completion_checkpoint(max_tokens)
                )
        if step.get("output_schema") is not None:
            profile += ", structured-output"
        if step.get("items_pointer") is not None:
            profile += ", json-items"
        lines.append(
            "- %s [%s, risk=%s%s]"
            % (redact_text(step["id"]), step["kind"], effective.get("risk", "low"), profile)
        )
    return "\n".join(lines)


def workflow_fingerprint(workflow: Dict) -> str:
    data = {key: value for key, value in workflow.items() if not key.startswith("_")}
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def iter_workflow_files(paths: Iterable[Path]) -> List[Path]:
    files = []
    for path in paths:
        reject_symlink_path(path, "workflow path")
        if path.is_dir():
            files.extend(_workflow_json_files_in_directory(path))
        else:
            files.append(path)
    return files


def _workflow_json_files_in_directory(path: Path) -> List[Path]:
    directory_fd = open_dir_no_follow(path, "workflow directory")
    try:
        files = []
        for name in os.listdir(directory_fd):
            if not name.endswith(".json"):
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                files.append(path / name)
        return sorted(files)
    finally:
        os.close(directory_fd)
