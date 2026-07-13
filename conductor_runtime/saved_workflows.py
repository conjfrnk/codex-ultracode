import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .claude_saved_workflows import compile_claude_saved_workflow
from .errors import ValidationError
from .redaction import redact_public_workflow_value, contains_secret_like, redact_text
from .security import ensure_dir_no_follow, open_dir_no_follow, reject_symlink_path, require_no_path_escape, resolve_under
from .workflow import SAFE_ID, validate_workflow


SAVED_WORKFLOW_EXTENSIONS = {".js", ".mjs"}
PROJECT_WORKFLOWS_DIR = ".claude/workflows"
PERSONAL_WORKFLOWS_DIR = ".claude/workflows"
MAX_SAVED_WORKFLOW_BYTES = 1024 * 1024
MAX_SAVED_WORKFLOW_ARGS = 32
MAX_SAVED_WORKFLOW_ARG_BYTES = 16 * 1024
MAX_RENDERED_WORKFLOW_BYTES = 2 * 1024 * 1024
PERSONAL_RANK_OFFSET = 100000
ARG_PLACEHOLDER = re.compile(r"\{\{\s*args\.([a-zA-Z0-9][a-zA-Z0-9_.-]*)\s*\}\}")


@dataclass(frozen=True)
class SavedWorkflow:
    command_name: str
    description: str
    when_to_use: str
    path: Path
    workflow: Dict
    scope: str = "project"
    rank: int = 0


@dataclass(frozen=True)
class SavedWorkflowFile:
    path: Path
    scope: str
    rank: int


def iter_saved_workflow_files(
    workspace: Path,
    workflow_dirs: Optional[Iterable[Path]] = None,
    project_root: Optional[Path] = None,
    include_personal: bool = False,
    home: Optional[Path] = None,
    personal_workflows_dir: Optional[Path] = None,
) -> List[Path]:
    return [
        entry.path
        for entry in iter_saved_workflow_entries(
            workspace=workspace,
            workflow_dirs=workflow_dirs,
            project_root=project_root,
            include_personal=include_personal,
            home=home,
            personal_workflows_dir=personal_workflows_dir,
        )
    ]


def iter_saved_workflow_entries(
    workspace: Path,
    workflow_dirs: Optional[Iterable[Path]] = None,
    project_root: Optional[Path] = None,
    include_personal: bool = False,
    home: Optional[Path] = None,
    personal_workflows_dir: Optional[Path] = None,
) -> List[SavedWorkflowFile]:
    base = workspace.resolve()
    locations = _saved_workflow_locations(
        base=base,
        workflow_dirs=workflow_dirs,
        project_root=project_root,
        include_personal=include_personal,
        home=home,
        personal_workflows_dir=personal_workflows_dir,
    )
    entries: List[SavedWorkflowFile] = []
    for directory, scope, rank in locations:
        for extension in sorted(SAVED_WORKFLOW_EXTENSIONS):
            for path in sorted(directory.glob("*%s" % extension)):
                entries.append(SavedWorkflowFile(path=path, scope=scope, rank=rank))
    return entries


def load_saved_workflow(
    path: Path,
    workspace: Optional[Path] = None,
    scope: str = "project",
    rank: int = 0,
) -> SavedWorkflow:
    source = _resolve_saved_workflow_path(path, workspace)
    reject_symlink_path(source, "saved workflow")
    if source.suffix not in SAVED_WORKFLOW_EXTENSIONS:
        raise ValidationError("saved workflow scripts must use .js or .mjs: %s" % path)
    text = _read_regular_text_file(source, "saved workflow", MAX_SAVED_WORKFLOW_BYTES)
    if _find_export_assignment(text, "workflow") is not None:
        meta, meta_span = _extract_json_export(text, "meta", source)
        workflow, workflow_span = _extract_json_export(text, "workflow", source)
        _reject_extra_script_text(text, [meta_span, workflow_span], source)
    else:
        meta, workflow = compile_claude_saved_workflow(text, source)
    command_name = meta.get("name")
    if not isinstance(command_name, str) or not SAFE_ID.match(command_name):
        raise ValidationError("%s meta.name must be a safe non-empty identifier" % source)
    if contains_secret_like(command_name):
        raise ValidationError("%s meta.name must not contain secret-like values" % source)
    description = meta.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValidationError("%s meta.description must be a string when present" % source)
    if contains_secret_like(description):
        raise ValidationError("%s meta.description must not contain secret-like values" % source)
    when_to_use = meta.get("whenToUse", "")
    if when_to_use is None:
        when_to_use = ""
    if not isinstance(when_to_use, str):
        raise ValidationError("%s meta.whenToUse must be a string when present" % source)
    if contains_secret_like(when_to_use):
        raise ValidationError("%s meta.whenToUse must not contain secret-like values" % source)
    validate_workflow(_workflow_template_for_validation(workflow), source=str(source))
    loaded = dict(workflow)
    loaded["_source_path"] = str(source)
    loaded["_saved_command"] = command_name
    return SavedWorkflow(
        command_name=command_name,
        description=description,
        when_to_use=when_to_use,
        path=source,
        workflow=loaded,
        scope=scope,
        rank=rank,
    )


def resolve_saved_workflow(
    identifier: str,
    workspace: Path,
    project_root: Optional[Path] = None,
    include_personal: bool = False,
    home: Optional[Path] = None,
    personal_workflows_dir: Optional[Path] = None,
) -> SavedWorkflow:
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValidationError("saved workflow name or path must be non-empty")
    value = identifier.strip()
    if _looks_like_saved_workflow_path(value):
        return load_saved_workflow(Path(value), workspace=workspace)
    if not SAFE_ID.match(value):
        raise ValidationError("saved workflow command names must be safe identifiers")
    matches: List[SavedWorkflow] = []
    invalid = []
    for entry in iter_saved_workflow_entries(
        workspace=workspace,
        project_root=project_root,
        include_personal=include_personal,
        home=home,
        personal_workflows_dir=personal_workflows_dir,
    ):
        try:
            saved = load_saved_workflow(entry.path, scope=entry.scope, rank=entry.rank)
        except Exception as exc:  # noqa: BLE001 - preserve invalid discovery diagnostics.
            invalid.append((entry.path, exc))
            continue
        if saved.command_name == value:
            matches.append(saved)
    if not matches:
        for path, exc in invalid:
            if path.stem == value:
                raise ValidationError("saved workflow %s is invalid: %s" % (value, exc))
        raise ValidationError("saved workflow not found: %s" % value)
    best_rank = min(match.rank for match in matches)
    best_matches = [match for match in matches if match.rank == best_rank]
    if len(best_matches) > 1:
        paths = ", ".join(str(match.path) for match in best_matches)
        raise ValidationError("saved workflow %s is ambiguous: %s" % (value, paths))
    return best_matches[0]


def parse_saved_workflow_args(raw_args: Iterable[str]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for raw in raw_args:
        if len(parsed) >= MAX_SAVED_WORKFLOW_ARGS:
            raise ValidationError("saved workflow args can be supplied at most %d times" % MAX_SAVED_WORKFLOW_ARGS)
        if not isinstance(raw, str) or "=" not in raw:
            raise ValidationError("saved workflow args must use KEY=VALUE")
        key, raw_value = raw.split("=", 1)
        key = key.strip()
        if not SAFE_ID.match(key):
            raise ValidationError("saved workflow arg keys must be safe identifiers")
        if contains_secret_like(key):
            raise ValidationError("saved workflow arg keys must not contain secret-like values")
        if key in parsed:
            raise ValidationError("duplicate saved workflow arg: %s" % key)
        if len(raw_value.encode("utf-8")) > MAX_SAVED_WORKFLOW_ARG_BYTES:
            raise ValidationError(
                "saved workflow arg %s must be at most %d bytes" % (key, MAX_SAVED_WORKFLOW_ARG_BYTES)
            )
        value = _parse_arg_value(raw_value)
        _reject_secret_like_arg_value(key, value)
        parsed[key] = value
    return parsed


def apply_saved_workflow_args(saved: SavedWorkflow, args: Dict[str, Any]) -> SavedWorkflow:
    workflow = {key: value for key, value in saved.workflow.items() if not key.startswith("_")}
    rendered, missing, used = _render_value(workflow, args)
    if missing:
        raise ValidationError(
            "saved workflow %s requires missing arg(s): %s"
            % (saved.command_name, ", ".join(sorted(missing)))
        )
    unused = sorted(set(args) - used)
    if unused:
        raise ValidationError(
            "saved workflow %s received unused arg(s): %s"
            % (saved.command_name, ", ".join(unused))
        )
    serialized = json.dumps(rendered, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_RENDERED_WORKFLOW_BYTES:
        raise ValidationError("rendered saved workflow must be at most %d bytes" % MAX_RENDERED_WORKFLOW_BYTES)
    validate_workflow(rendered, source=str(saved.path))
    loaded = dict(rendered)
    loaded["_source_path"] = str(saved.path)
    loaded["_saved_command"] = saved.command_name
    if args:
        loaded["_saved_args"] = sorted(args)
    return SavedWorkflow(
        command_name=saved.command_name,
        description=saved.description,
        when_to_use=saved.when_to_use,
        path=saved.path,
        workflow=loaded,
        scope=saved.scope,
        rank=saved.rank,
    )


def write_saved_workflow_json(saved: SavedWorkflow, destination: Path) -> Path:
    if destination.exists():
        raise ValidationError("destination already exists: %s" % destination)
    reject_symlink_path(destination, "saved workflow export")
    reject_symlink_path(destination.parent, "saved workflow export parent")
    workflow = {key: value for key, value in saved.workflow.items() if not key.startswith("_")}
    validate_workflow(workflow, source=str(saved.path))
    text = json.dumps(redact_public_workflow_value(workflow), indent=2, sort_keys=True) + "\n"
    _write_new_text_file_no_follow(destination, text)
    return destination


def write_saved_workflow_script(
    workflow: Dict,
    destination: Path,
    command_name: str,
    description: Optional[str] = None,
) -> Path:
    validate_saved_workflow_script_destination(destination)
    command_name = validate_saved_workflow_command_name(command_name)
    if description is None:
        description = workflow.get("description", "")
    if not isinstance(description, str):
        raise ValidationError("saved workflow description must be a string")
    if contains_secret_like(description):
        raise ValidationError("saved workflow description must not contain secret-like values")
    validate_workflow(workflow, source=str(destination))
    meta = {
        "name": command_name,
        "description": description,
    }
    text = (
        "export const meta = %s;\n\n"
        "export const workflow = %s;\n"
    ) % (
        json.dumps(meta, indent=2, sort_keys=True, allow_nan=False),
        json.dumps(workflow, indent=2, sort_keys=True, allow_nan=False),
    )
    if len(text.encode("utf-8")) > MAX_SAVED_WORKFLOW_BYTES:
        raise ValidationError("saved workflow script must be at most %d bytes" % MAX_SAVED_WORKFLOW_BYTES)
    _write_new_text_file_no_follow(destination, text, label="saved workflow output")
    return destination


def validate_saved_workflow_script_destination(destination: Path) -> Path:
    reject_symlink_path(destination, "saved workflow output")
    if destination.exists():
        raise ValidationError("destination already exists: %s" % destination)
    if destination.suffix not in SAVED_WORKFLOW_EXTENSIONS:
        raise ValidationError("saved workflow scripts must use .js or .mjs: %s" % destination)
    return destination


def validate_saved_workflow_command_name(command_name: str) -> str:
    if not isinstance(command_name, str) or not SAFE_ID.match(command_name):
        raise ValidationError("saved workflow command name must be a safe non-empty identifier")
    if contains_secret_like(command_name):
        raise ValidationError("saved workflow command name must not contain secret-like values")
    return command_name


def _saved_workflow_locations(
    base: Path,
    workflow_dirs: Optional[Iterable[Path]],
    project_root: Optional[Path],
    include_personal: bool,
    home: Optional[Path],
    personal_workflows_dir: Optional[Path],
) -> List[Tuple[Path, str, int]]:
    locations: List[Tuple[Path, str, int]] = []
    if workflow_dirs is not None:
        for rank, directory in enumerate(workflow_dirs):
            locations.append((_workspace_relative_workflow_dir(base, directory), "project", rank))
    else:
        root = _project_root(base, project_root)
        current = base
        rank = 0
        while True:
            locations.append((current / PROJECT_WORKFLOWS_DIR, "project", rank))
            if current == root:
                break
            current = current.parent
            rank += 1
    if include_personal:
        personal = _personal_workflow_dir(home=home, personal_workflows_dir=personal_workflows_dir)
        locations.append((personal, "personal", PERSONAL_RANK_OFFSET))

    existing: List[Tuple[Path, str, int]] = []
    seen = set()
    for directory, scope, rank in locations:
        reject_symlink_path(directory, "saved workflow directory")
        if not directory.exists():
            continue
        reject_symlink_path(directory, "saved workflow directory")
        if not directory.is_dir():
            raise ValidationError("saved workflow path is not a directory: %s" % directory)
        resolved = directory.resolve()
        key = (str(resolved), scope)
        if key in seen:
            continue
        seen.add(key)
        existing.append((resolved, scope, rank))
    return existing


def _workspace_relative_workflow_dir(base: Path, directory: Path) -> Path:
    if directory.is_absolute():
        raise ValidationError("saved workflow directories must be workspace-relative")
    require_no_path_escape(directory.as_posix())
    reject_symlink_path(base / directory, "saved workflow directory")
    return resolve_under(base, directory.as_posix())


def _project_root(base: Path, project_root: Optional[Path]) -> Path:
    if project_root is None:
        return base
    raw = project_root
    reject_symlink_path(raw, "project root")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise ValidationError("project root is not a directory: %s" % project_root)
    try:
        base.relative_to(resolved)
    except ValueError:
        raise ValidationError("workspace must be inside project root: %s" % project_root)
    return resolved


def _personal_workflow_dir(home: Optional[Path], personal_workflows_dir: Optional[Path]) -> Path:
    if personal_workflows_dir is not None:
        raw = personal_workflows_dir
        reject_symlink_path(raw, "personal saved workflow directory")
        return raw.resolve()
    raw_home = (home or Path.home())
    reject_symlink_path(raw_home, "home directory")
    return raw_home.resolve() / PERSONAL_WORKFLOWS_DIR


def _resolve_saved_workflow_path(path: Path, workspace: Optional[Path]) -> Path:
    if workspace is None:
        reject_symlink_path(path, "saved workflow")
        return path.resolve()
    base = workspace.resolve()
    if path.is_absolute():
        reject_symlink_path(path, "saved workflow")
        resolved = path.resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            raise ValidationError("saved workflow path escapes workspace: %s" % path)
        return resolved
    raw_path = base / path
    reject_symlink_path(raw_path, "saved workflow")
    require_no_path_escape(path.as_posix())
    return resolve_under(base, path.as_posix())


def _read_regular_text_file(path: Path, label: str, max_bytes: int) -> str:
    reject_symlink_path(path, label)
    parent_fd = open_dir_no_follow(path.parent, "%s parent" % label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise FileNotFoundError("missing %s script: %s" % (label, path))
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, path, exc.__class__.__name__))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        if info.st_size > max_bytes:
            raise ValidationError("%s script must be at most %d bytes" % (label, max_bytes))
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValidationError("%s script must be at most %d bytes" % (label, max_bytes))
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("%s must be valid UTF-8: %s" % (label, path))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _write_new_text_file_no_follow(destination: Path, text: str, label: str = "saved workflow export") -> None:
    parent = destination.parent
    parent_label = "%s parent" % label
    reject_symlink_path(parent, parent_label)
    parent_fd = ensure_dir_no_follow(parent, parent_label)
    fd = None
    try:
        parent_info = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode):
            raise ValidationError("%s must be a directory: %s" % (parent_label, parent))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(destination.name, flags, 0o600, dir_fd=parent_fd)
        payload = text.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
    except FileExistsError:
        raise ValidationError("destination already exists: %s" % destination)
    except OSError as exc:
        try:
            os.unlink(destination.name, dir_fd=parent_fd)
        except OSError:
            pass
        raise ValidationError("failed to write %s %s: %s" % (label, destination, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _parse_arg_value(raw_value: str) -> Any:
    stripped = raw_value.strip()
    if stripped:
        try:
            return json.loads(
                stripped,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except json.JSONDecodeError:
            pass
        except ValueError as exc:
            raise ValidationError("saved workflow arg contains invalid JSON: %s" % exc)
    return raw_value


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constants are not supported: %s" % value)


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key %s" % key)
        value[key] = item
    return value


def _reject_secret_like_arg_value(key: str, value: Any) -> None:
    rendered = _arg_value_text(value)
    if contains_secret_like(rendered):
        raise ValidationError("saved workflow arg %s must not contain secret-like values" % key)


def _arg_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _render_value(value: Any, args: Dict[str, Any]) -> Tuple[Any, set, set]:
    missing = set()
    used = set()
    if isinstance(value, str):
        matches = list(ARG_PLACEHOLDER.finditer(value))
        if not matches:
            return value, missing, used
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            key = matches[0].group(1)
            if key not in args:
                return value, {key}, used
            return args[key], missing, {key}

        chunks = []
        previous_end = 0
        for match in matches:
            chunks.append(value[previous_end:match.start()])
            key = match.group(1)
            if key not in args:
                missing.add(key)
                chunks.append(match.group(0))
                previous_end = match.end()
                continue
            chunks.append(_arg_value_text(args[key]))
            used.add(key)
            previous_end = match.end()
        chunks.append(value[previous_end:])
        return redact_text("".join(chunks)), missing, used
    if isinstance(value, list):
        rendered_items = []
        for item in value:
            rendered_item, item_missing, item_used = _render_value(item, args)
            rendered_items.append(rendered_item)
            missing.update(item_missing)
            used.update(item_used)
        return rendered_items, missing, used
    if isinstance(value, dict):
        rendered_dict = {}
        for key, item in value.items():
            rendered_item, item_missing, item_used = _render_value(item, args)
            rendered_dict[key] = rendered_item
            missing.update(item_missing)
            used.update(item_used)
        return rendered_dict, missing, used
    return value, missing, used


def _workflow_template_for_validation(workflow: Dict) -> Dict:
    rendered, _missing, _used = _template_value(workflow, context_key="")
    return rendered


def _template_value(value: Any, context_key: str) -> Tuple[Any, set, set]:
    if isinstance(value, str):
        matches = list(ARG_PLACEHOLDER.finditer(value))
        if not matches:
            return value, set(), set()
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return _placeholder_validation_value(context_key), set(), {matches[0].group(1)}
        rendered = ARG_PLACEHOLDER.sub("placeholder", value)
        return rendered, set(), {match.group(1) for match in matches}
    if isinstance(value, list):
        items = []
        used = set()
        for item in value:
            rendered, _missing, item_used = _template_value(item, context_key=context_key)
            items.append(rendered)
            used.update(item_used)
        return items, set(), used
    if isinstance(value, dict):
        rendered = {}
        used = set()
        for key, item in value.items():
            context_key = str(key)
            if key == "items" and value.get("item_semantics") == "json":
                context_key = "json_items"
            item_rendered, _missing, item_used = _template_value(
                item,
                context_key=context_key,
            )
            rendered[key] = item_rendered
            used.update(item_used)
        return rendered, set(), used
    return value, set(), set()


def _placeholder_validation_value(context_key: str) -> Any:
    if context_key in {
        "max_workers",
        "max_items",
        "output_limit_bytes",
        "default_timeout_seconds",
        "agent_timeout_seconds",
        "timeout_seconds",
    }:
        return 1
    if context_key in {"writes", "destructive", "network", "external_path"}:
        return False
    if context_key == "items":
        return ["placeholder"]
    if context_key == "json_items":
        return [{"placeholder": "placeholder"}]
    if context_key == "command":
        return ["true"]
    if context_key == "depends_on":
        return []
    return "placeholder"


def _extract_json_export(text: str, name: str, source: Path):
    assignment = _find_export_assignment(text, name)
    if assignment is None:
        raise ValidationError("%s must export const %s as a JSON-compatible object" % (source, name))
    start, index = assignment
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "{":
        raise ValidationError("%s export %s must start with a JSON object" % (source, name))
    end = _object_end(text, index, source, name)
    literal = text[index:end]
    try:
        data = json.loads(literal)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s export %s must be valid JSON: %s" % (source, name, exc.msg))
    if not isinstance(data, dict):
        raise ValidationError("%s export %s must be a JSON object" % (source, name))
    statement_end = end
    while statement_end < len(text) and text[statement_end].isspace():
        statement_end += 1
    if statement_end < len(text) and text[statement_end] == ";":
        statement_end += 1
    return data, (start, statement_end)


def _find_export_assignment(text: str, name: str):
    index = 0
    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if char in {"\n", "\r"}:
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        if char in {"\"", "'", "`"}:
            in_string = char
            index += 1
            continue
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth = max(depth - 1, 0)
            index += 1
            continue
        if depth == 0 and _word_at(text, index, "export"):
            start = index
            index += len("export")
            index = _skip_space(text, index)
            if not _word_at(text, index, "const"):
                continue
            index += len("const")
            index = _skip_space(text, index)
            if not _word_at(text, index, name):
                continue
            index += len(name)
            index = _skip_space(text, index)
            if index < len(text) and text[index] == "=":
                return start, index + 1
            continue
        index += 1
    return None


def _word_at(text: str, index: int, word: str) -> bool:
    if not text.startswith(word, index):
        return False
    before = text[index - 1] if index > 0 else ""
    after_index = index + len(word)
    after = text[after_index] if after_index < len(text) else ""
    return not _identifier_char(before) and not _identifier_char(after)


def _identifier_char(value: str) -> bool:
    return bool(value) and (value.isalnum() or value in {"_", "$"})


def _skip_space(text: str, index: int) -> int:
    if index >= len(text) or not text[index].isspace():
        return index
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _reject_extra_script_text(text: str, spans, source: Path) -> None:
    allowed = [False] * len(text)
    for start, end in spans:
        for index in range(start, min(end, len(text))):
            allowed[index] = True
    for index, char in enumerate(text):
        if allowed[index] or char.isspace():
            continue
        raise ValidationError(
            "%s may contain only JSON-compatible `export const meta` and `export const workflow` statements"
            % source
        )


def _object_end(text: str, start: int, source: Path, name: str) -> int:
    depth = 0
    in_string = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {"\"", "'"}:
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValidationError("%s export %s has an unterminated object" % (source, name))


def _looks_like_saved_workflow_path(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
    )
