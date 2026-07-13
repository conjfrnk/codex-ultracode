"""Static compiler for a safe, useful subset of Claude saved workflows."""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .errors import ValidationError
from .workflow import SAFE_ID, SCHEMA


MAX_CLAUDE_WORKFLOW_BINDINGS = 64
MAX_CLAUDE_WORKFLOW_DEPTH = 32
MAX_CLAUDE_WORKFLOW_TOKENS = 100000
MAX_CLAUDE_WORKFLOW_AGENTS = 1000
MAX_CLAUDE_WORKFLOW_WORKERS = 16
_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_JS_REFERENCE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$"
)
_JS_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)


@dataclass(frozen=True)
class Reference:
    parts: Tuple[str, ...]


@dataclass(frozen=True)
class Template:
    parts: Tuple[Union[str, Reference], ...]


@dataclass(frozen=True)
class AgentCall:
    prompt: Any
    options: Dict[str, Any]


@dataclass(frozen=True)
class PipelineCall:
    source: Any
    item_name: str
    agent: AgentCall


@dataclass(frozen=True)
class Binding:
    name: str
    expression: Union[AgentCall, PipelineCall]


@dataclass(frozen=True)
class Program:
    meta: Dict[str, Any]
    bindings: Tuple[Binding, ...]
    return_name: str
    filters_falsey: bool


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    offset: int


def compile_claude_saved_workflow(text: str, source: Path) -> Tuple[Dict, Dict]:
    """Compile the supported Claude syntax without evaluating JavaScript."""

    program = _Parser(text, source).parse()
    workflow = _compile_program(program, source)
    return program.meta, workflow


class _Scanner:
    def __init__(self, text: str, source: Path):
        self.text = text
        self.source = source
        self.index = 0
        self.tokens: List[Token] = []

    def scan(self) -> List[Token]:
        while self.index < len(self.text):
            self._skip_space_and_comments()
            if self.index >= len(self.text):
                break
            offset = self.index
            char = self.text[self.index]
            if char in {"'", '"'}:
                self.tokens.append(Token("string", self._scan_string(char), offset))
            elif char == "`":
                self.tokens.append(Token("template", self._scan_template(), offset))
            elif char.isalpha() or char in {"_", "$"}:
                self.tokens.append(Token("identifier", self._scan_identifier(), offset))
            elif char.isdigit() or (char == "-" and self._peek(1).isdigit()):
                self.tokens.append(Token("number", self._scan_number(), offset))
            elif self.text.startswith("=>", self.index):
                self.tokens.append(Token("symbol", "=>", offset))
                self.index += 2
            elif char in "{}[](),:;.=":
                self.tokens.append(Token("symbol", char, offset))
                self.index += 1
            else:
                self._fail(offset, "unsupported JavaScript token %r" % char)
            if len(self.tokens) > MAX_CLAUDE_WORKFLOW_TOKENS:
                self._fail(offset, "contains too many tokens")
        self.tokens.append(Token("eof", None, len(self.text)))
        return self.tokens

    def _skip_space_and_comments(self) -> None:
        while self.index < len(self.text):
            if self.text[self.index].isspace():
                self.index += 1
                continue
            if self.text.startswith("//", self.index):
                newline = self.text.find("\n", self.index + 2)
                self.index = len(self.text) if newline < 0 else newline + 1
                continue
            if self.text.startswith("/*", self.index):
                end = self.text.find("*/", self.index + 2)
                if end < 0:
                    self._fail(self.index, "has an unterminated block comment")
                self.index = end + 2
                continue
            return

    def _scan_identifier(self) -> str:
        start = self.index
        self.index += 1
        while self.index < len(self.text):
            char = self.text[self.index]
            if not (char.isalnum() or char in {"_", "$"}):
                break
            self.index += 1
        return self.text[start:self.index]

    def _scan_number(self) -> Union[int, float]:
        match = _JS_NUMBER.match(self.text, self.index)
        if match is None:
            self._fail(self.index, "has an invalid number")
        raw = match.group(0)
        self.index = match.end()
        try:
            value = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            self._fail(match.start(), "has an invalid number")
        if isinstance(value, float) and not math.isfinite(value):
            self._fail(match.start(), "has a non-finite number")
        return value

    def _scan_string(self, quote: str) -> str:
        start = self.index
        self.index += 1
        output: List[str] = []
        while self.index < len(self.text):
            char = self.text[self.index]
            if char == quote:
                self.index += 1
                return "".join(output)
            if char in {"\n", "\r"}:
                self._fail(start, "has an unterminated string")
            if char == "\\":
                output.append(self._scan_escape(quote, template=False))
            else:
                output.append(char)
                self.index += 1
        self._fail(start, "has an unterminated string")

    def _scan_template(self) -> Template:
        start = self.index
        self.index += 1
        parts: List[Union[str, Reference]] = []
        literal: List[str] = []
        while self.index < len(self.text):
            char = self.text[self.index]
            if char == "`":
                self.index += 1
                parts.append("".join(literal))
                return Template(tuple(parts))
            if char == "\\":
                literal.append(self._scan_escape("`", template=True))
                continue
            if self.text.startswith("${", self.index):
                parts.append("".join(literal))
                literal = []
                expression_start = self.index + 2
                expression_end = self.text.find("}", expression_start)
                if expression_end < 0:
                    self._fail(start, "has an unterminated template interpolation")
                expression = self.text[expression_start:expression_end].strip()
                if not _JS_REFERENCE.fullmatch(expression):
                    self._fail(
                        expression_start,
                        "template interpolations may contain only dotted identifiers",
                    )
                parts.append(Reference(tuple(expression.split("."))))
                self.index = expression_end + 1
                continue
            literal.append(char)
            self.index += 1
        self._fail(start, "has an unterminated template literal")

    def _scan_escape(self, quote: str, template: bool) -> str:
        offset = self.index
        self.index += 1
        if self.index >= len(self.text):
            self._fail(offset, "has an unterminated escape sequence")
        char = self.text[self.index]
        self.index += 1
        simple = {
            "\\": "\\",
            "'": "'",
            '"': '"',
            "`": "`",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "f": "\f",
            "v": "\v",
        }
        if char in simple:
            return simple[char]
        if template and char in {"$", "{"}:
            return char
        if char in {"\n", "\r"}:
            if char == "\r" and self._peek() == "\n":
                self.index += 1
            return ""
        if char in {"u", "x"}:
            width = 4 if char == "u" else 2
            raw = self.text[self.index:self.index + width]
            if len(raw) != width or any(value not in "0123456789abcdefABCDEF" for value in raw):
                self._fail(offset, "has an invalid hexadecimal escape")
            self.index += width
            codepoint = int(raw, 16)
            if 0xD800 <= codepoint <= 0xDFFF:
                self._fail(offset, "must not contain an unpaired surrogate escape")
            return chr(codepoint)
        self._fail(offset, "uses an unsupported escape sequence")

    def _peek(self, distance: int = 0) -> str:
        index = self.index + distance
        return self.text[index] if index < len(self.text) else ""

    def _fail(self, offset: int, message: str):
        raise ValidationError(_diagnostic(self.text, self.source, offset, message))


class _Parser:
    def __init__(self, text: str, source: Path):
        self.text = text
        self.source = source
        self.tokens = _Scanner(text, source).scan()
        self.index = 0

    def parse(self) -> Program:
        meta = None
        bindings: List[Binding] = []
        names = set()
        return_name = None
        filters_falsey = False
        while not self._at("eof"):
            if self._accept_symbol(";"):
                continue
            if return_name is not None:
                self._fail("must not contain statements after return")
            if self._at_identifier("export"):
                if meta is not None:
                    self._fail("must export meta exactly once")
                meta = self._parse_meta()
                continue
            if self._at_identifier("const"):
                binding = self._parse_binding()
                if binding.name in names:
                    self._fail("redeclares variable %s" % binding.name)
                if binding.name in {"args", "meta"}:
                    self._fail("must not bind reserved variable %s" % binding.name)
                if not SAFE_ID.fullmatch(binding.name):
                    self._fail("binding names must be safe Conductor identifiers")
                names.add(binding.name)
                bindings.append(binding)
                if len(bindings) > MAX_CLAUDE_WORKFLOW_BINDINGS:
                    self._fail(
                        "may define at most %d agent or pipeline bindings"
                        % MAX_CLAUDE_WORKFLOW_BINDINGS
                    )
                continue
            if self._at_identifier("return"):
                return_name, filters_falsey = self._parse_return()
                continue
            self._fail(
                "supports only `export const meta`, awaited agent/pipeline bindings, and return"
            )
        if meta is None:
            self._fail("must export const meta")
        if not bindings:
            self._fail("must define at least one awaited agent or pipeline binding")
        if return_name is None:
            self._fail("must end with a return statement")
        if return_name not in names:
            self._fail("returns unknown variable %s" % return_name)
        return Program(meta, tuple(bindings), return_name, filters_falsey)

    def _parse_meta(self) -> Dict[str, Any]:
        self._expect_identifier("export")
        self._expect_identifier("const")
        self._expect_identifier("meta")
        self._expect_symbol("=")
        value = self._parse_value()
        self._accept_symbol(";")
        if not isinstance(value, dict) or _contains_expression(value):
            self._fail("export const meta must be a static object literal")
        return value

    def _parse_binding(self) -> Binding:
        self._expect_identifier("const")
        name = self._expect_kind("identifier").value
        self._expect_symbol("=")
        self._expect_identifier("await")
        call = self._expect_kind("identifier")
        if call.value == "agent":
            expression = self._parse_agent_call(call_consumed=True)
        elif call.value == "pipeline":
            expression = self._parse_pipeline_call()
        else:
            self._fail("awaited calls may use only agent() or pipeline()", call)
        self._accept_symbol(";")
        return Binding(name, expression)

    def _parse_agent_call(self, call_consumed: bool = False) -> AgentCall:
        if not call_consumed:
            self._expect_identifier("agent")
        self._expect_symbol("(")
        prompt = self._parse_value()
        options: Dict[str, Any] = {}
        if self._accept_symbol(","):
            if self._at_symbol(")"):
                self._expect_symbol(")")
                return AgentCall(prompt, options)
            options_value = self._parse_value()
            if not isinstance(options_value, dict):
                self._fail("agent options must be an object literal")
            options = options_value
            self._accept_symbol(",")
        self._expect_symbol(")")
        return AgentCall(prompt, options)

    def _parse_pipeline_call(self) -> PipelineCall:
        self._expect_symbol("(")
        source = self._parse_value()
        self._expect_symbol(",")
        self._accept_identifier("async")
        if self._accept_symbol("("):
            item_name = self._expect_kind("identifier").value
            self._expect_symbol(")")
        else:
            item_name = self._expect_kind("identifier").value
        if not _JS_IDENTIFIER.fullmatch(item_name):
            self._fail("pipeline callback parameter must be an identifier")
        self._expect_symbol("=>")
        self._accept_identifier("await")
        agent = self._parse_agent_call()
        self._accept_symbol(",")
        self._expect_symbol(")")
        return PipelineCall(source, item_name, agent)

    def _parse_return(self) -> Tuple[str, bool]:
        self._expect_identifier("return")
        name = self._expect_kind("identifier").value
        filters_falsey = False
        if self._accept_symbol("."):
            self._expect_identifier("filter")
            self._expect_symbol("(")
            self._expect_identifier("Boolean")
            self._expect_symbol(")")
            filters_falsey = True
        self._accept_symbol(";")
        return name, filters_falsey

    def _parse_value(self, depth: int = 0) -> Any:
        if depth > MAX_CLAUDE_WORKFLOW_DEPTH:
            self._fail("literal nesting is too deep")
        token = self._current()
        if token.kind in {"string", "template", "number"}:
            self.index += 1
            return token.value
        if token.kind == "identifier":
            if token.value == "true":
                self.index += 1
                return True
            if token.value == "false":
                self.index += 1
                return False
            if token.value == "null":
                self.index += 1
                return None
            return self._parse_reference()
        if self._accept_symbol("["):
            values = []
            while not self._accept_symbol("]"):
                values.append(self._parse_value(depth + 1))
                if self._accept_symbol("]"):
                    break
                self._expect_symbol(",")
                if self._accept_symbol("]"):
                    break
            return values
        if self._accept_symbol("{"):
            value: Dict[str, Any] = {}
            while not self._accept_symbol("}"):
                key_token = self._current()
                if key_token.kind not in {"identifier", "string"}:
                    self._fail("object keys must be identifiers or strings")
                self.index += 1
                key = key_token.value
                if key in value:
                    self._fail("object literal contains duplicate key %s" % key, key_token)
                self._expect_symbol(":")
                value[key] = self._parse_value(depth + 1)
                if self._accept_symbol("}"):
                    break
                self._expect_symbol(",")
                if self._accept_symbol("}"):
                    break
            return value
        self._fail("expected a static literal or dotted reference")

    def _parse_reference(self) -> Reference:
        parts = [self._expect_kind("identifier").value]
        while self._accept_symbol("."):
            parts.append(self._expect_kind("identifier").value)
        return Reference(tuple(parts))

    def _current(self) -> Token:
        return self.tokens[self.index]

    def _at(self, kind: str) -> bool:
        return self._current().kind == kind

    def _at_identifier(self, value: str) -> bool:
        token = self._current()
        return token.kind == "identifier" and token.value == value

    def _accept_identifier(self, value: str) -> bool:
        if not self._at_identifier(value):
            return False
        self.index += 1
        return True

    def _expect_identifier(self, value: str) -> Token:
        if not self._at_identifier(value):
            self._fail("expected %s" % value)
        token = self._current()
        self.index += 1
        return token

    def _at_symbol(self, value: str) -> bool:
        token = self._current()
        return token.kind == "symbol" and token.value == value

    def _accept_symbol(self, value: str) -> bool:
        if not self._at_symbol(value):
            return False
        self.index += 1
        return True

    def _expect_symbol(self, value: str) -> Token:
        if not self._at_symbol(value):
            self._fail("expected %s" % value)
        token = self._current()
        self.index += 1
        return token

    def _expect_kind(self, kind: str) -> Token:
        if not self._at(kind):
            self._fail("expected %s" % kind)
        token = self._current()
        self.index += 1
        return token

    def _fail(self, message: str, token: Optional[Token] = None):
        current = token or self._current()
        raise ValidationError(_diagnostic(self.text, self.source, current.offset, message))


def _compile_program(program: Program, source: Path) -> Dict:
    command_name = program.meta.get("name")
    description = program.meta.get("description", "")
    if not isinstance(command_name, str) or not SAFE_ID.fullmatch(command_name):
        raise ValidationError("%s meta.name must be a safe non-empty identifier" % source)
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValidationError("%s meta.description must be a string when present" % source)
    unsupported_meta = sorted(set(program.meta) - {"name", "description"})
    if unsupported_meta:
        raise ValidationError(
            "%s Claude-style meta contains unsupported field(s): %s"
            % (source, ", ".join(unsupported_meta))
        )

    bindings = {binding.name: binding for binding in program.bindings}
    consumed_properties: Dict[str, str] = {}
    direct_agents = sum(isinstance(binding.expression, AgentCall) for binding in program.bindings)
    pipelines = sum(isinstance(binding.expression, PipelineCall) for binding in program.bindings)
    if direct_agents >= MAX_CLAUDE_WORKFLOW_AGENTS:
        raise ValidationError("%s Claude-style workflow leaves no capacity for pipeline agents" % source)
    per_pipeline_items = (
        (MAX_CLAUDE_WORKFLOW_AGENTS - direct_agents) // pipelines if pipelines else 0
    )

    for binding in program.bindings:
        expression = binding.expression
        if not isinstance(expression, PipelineCall):
            continue
        reference = expression.source
        if isinstance(reference, Reference) and len(reference.parts) == 2:
            owner, property_name = reference.parts
            if owner == "args":
                continue
            owner_binding = bindings.get(owner)
            if owner_binding is None or not isinstance(owner_binding.expression, AgentCall):
                raise ValidationError(
                    "%s pipeline %s source must reference args or a prior agent result"
                    % (source, binding.name)
                )
            previous = consumed_properties.get(owner)
            if previous is not None and previous != property_name:
                raise ValidationError(
                    "%s agent result %s cannot feed multiple schema properties" % (source, owner)
                )
            consumed_properties[owner] = property_name

    steps = []
    prior_step = None
    seen_bindings = set()
    for binding in program.bindings:
        expression = binding.expression
        dependencies = [prior_step] if prior_step is not None else []
        if isinstance(expression, AgentCall):
            options = _agent_options(expression.options, source, binding.name)
            prompt = _agent_prompt(expression.prompt, source, binding.name)
            property_name = consumed_properties.get(binding.name)
            if property_name is not None:
                _validate_string_array_schema(options.get("schema"), property_name, source, binding.name)
            step = {
                "id": binding.name,
                "kind": "codex_exec",
                "risk": "low",
                "sandbox": "read-only",
                "prompt": prompt,
                "capture": _agent_capture(binding.name, options.get("schema") is not None),
            }
            if options.get("schema") is not None:
                step["output_schema"] = _normalize_output_schema(options["schema"])
        else:
            if per_pipeline_items < 1:
                raise ValidationError("%s Claude-style workflow exceeds its 1,000-agent cap" % source)
            options = _agent_options(
                expression.agent.options,
                source,
                binding.name,
                label_reference=expression.item_name,
            )
            prompt_template = _pipeline_prompt(
                expression.agent.prompt,
                expression.item_name,
                source,
                binding.name,
            )
            item_source, source_dependency = _pipeline_source(
                expression.source,
                binding.name,
                bindings,
                seen_bindings,
                per_pipeline_items,
                source,
            )
            if source_dependency is not None and source_dependency not in dependencies:
                dependencies.append(source_dependency)
            step = {
                "id": binding.name,
                "kind": "agent_map",
                "risk": "low",
                "sandbox": "read-only",
                "prompt_template": prompt_template,
                "capture_dir": "claude-workflow/%s" % binding.name,
                "max_items": per_pipeline_items,
                "max_workers": MAX_CLAUDE_WORKFLOW_WORKERS,
                "preserve_duplicate_items": True,
            }
            if options.get("schema") is not None:
                step["output_schema"] = _normalize_output_schema(options["schema"])
            step.update(item_source)
        if dependencies:
            step["depends_on"] = dependencies
        steps.append(step)
        prior_step = binding.name
        seen_bindings.add(binding.name)

    returned = bindings[program.return_name].expression
    if program.filters_falsey and not isinstance(returned, PipelineCall):
        raise ValidationError("%s filter(Boolean) may be used only on a pipeline result" % source)
    return {
        "schema": SCHEMA,
        "name": command_name,
        "description": description,
        "mode": "read_only",
        "max_workers": MAX_CLAUDE_WORKFLOW_WORKERS if pipelines else 1,
        "max_items": per_pipeline_items if pipelines else MAX_CLAUDE_WORKFLOW_AGENTS,
        "steps": steps,
    }


def _agent_options(
    options: Dict[str, Any],
    source: Path,
    binding_name: str,
    label_reference: Optional[str] = None,
) -> Dict[str, Any]:
    unsupported = sorted(set(options) - {"label", "schema"})
    if unsupported:
        raise ValidationError(
            "%s %s agent options contain unsupported field(s): %s"
            % (source, binding_name, ", ".join(unsupported))
        )
    if "label" in options:
        label = options["label"]
        if not isinstance(label, (str, Template, Reference)):
            raise ValidationError("%s %s agent label must be static text or a reference" % (source, binding_name))
        references = []
        if isinstance(label, Reference):
            references.append(label)
        elif isinstance(label, Template):
            references.extend(part for part in label.parts if isinstance(part, Reference))
        for reference in references:
            if reference.parts == (label_reference,) and label_reference is not None:
                continue
            if len(reference.parts) == 2 and reference.parts[0] == "args":
                continue
            raise ValidationError(
                "%s %s agent label may reference only args.NAME%s"
                % (
                    source,
                    binding_name,
                    " or %s" % label_reference if label_reference is not None else "",
                )
            )
    if "schema" in options and (
        not isinstance(options["schema"], dict) or _contains_expression(options["schema"])
    ):
        raise ValidationError("%s %s agent schema must be a static object literal" % (source, binding_name))
    return options


def _agent_prompt(value: Any, source: Path, binding_name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Reference) and len(value.parts) == 2 and value.parts[0] == "args":
        return "{{args.%s}}" % value.parts[1]
    if isinstance(value, Template):
        output = []
        for part in value.parts:
            if isinstance(part, str):
                output.append(part)
            elif len(part.parts) == 2 and part.parts[0] == "args":
                output.append("{{args.%s}}" % part.parts[1])
            else:
                raise ValidationError(
                    "%s %s agent prompt templates may interpolate only args.NAME"
                    % (source, binding_name)
                )
        return "".join(output)
    raise ValidationError("%s %s agent prompt must be text or args.NAME" % (source, binding_name))


def _pipeline_prompt(value: Any, item_name: str, source: Path, binding_name: str) -> str:
    if not isinstance(value, Template):
        raise ValidationError(
            "%s %s pipeline agent prompt must be a template literal using ${%s}"
            % (source, binding_name, item_name)
        )
    output = []
    item_references = 0
    for part in value.parts:
        if isinstance(part, str):
            output.append(part.replace("{", "{{").replace("}", "}}"))
        elif part.parts == (item_name,):
            output.append("{item}")
            item_references += 1
        else:
            raise ValidationError(
                "%s %s pipeline prompt may interpolate only its %s callback parameter"
                % (source, binding_name, item_name)
            )
    if item_references == 0:
        raise ValidationError(
            "%s %s pipeline prompt must interpolate its %s callback parameter"
            % (source, binding_name, item_name)
        )
    return "".join(output)


def _pipeline_source(
    value: Any,
    binding_name: str,
    bindings: Dict[str, Binding],
    seen_bindings: set,
    max_items: int,
    source: Path,
) -> Tuple[Dict[str, Any], Optional[str]]:
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) and item for item in value):
            raise ValidationError("%s %s pipeline literal source must be a non-empty string array" % (source, binding_name))
        if len(value) > max_items:
            raise ValidationError("%s %s pipeline literal source exceeds its agent budget" % (source, binding_name))
        return {"items": value}, None
    if not isinstance(value, Reference) or len(value.parts) != 2:
        raise ValidationError(
            "%s %s pipeline source must be a string array, args.NAME, or priorAgent.property"
            % (source, binding_name)
        )
    owner, property_name = value.parts
    if owner == "args":
        return {"items": "{{args.%s}}" % property_name}, None
    owner_binding = bindings.get(owner)
    if owner_binding is None or not isinstance(owner_binding.expression, AgentCall):
        raise ValidationError("%s %s pipeline source references unknown agent %s" % (source, binding_name, owner))
    if owner not in seen_bindings:
        raise ValidationError("%s %s pipeline source must reference a prior agent" % (source, binding_name))
    _validate_string_array_schema(owner_binding.expression.options.get("schema"), property_name, source, owner)
    pointer = "/" + property_name.replace("~", "~0").replace("/", "~1")
    return {
        "items_artifact": _agent_capture(owner, structured=True),
        "items_pointer": pointer,
    }, owner


def _validate_string_array_schema(schema: Any, property_name: str, source: Path, binding_name: str) -> None:
    valid = isinstance(schema, dict) and schema.get("type") == "object"
    properties = schema.get("properties") if valid else None
    property_schema = properties.get(property_name) if isinstance(properties, dict) else None
    valid = (
        valid
        and isinstance(property_schema, dict)
        and property_schema.get("type") == "array"
        and isinstance(property_schema.get("items"), dict)
        and property_schema["items"].get("type") == "string"
    )
    required = schema.get("required") if isinstance(schema, dict) else None
    valid = valid and isinstance(required, list) and property_name in required
    if not valid:
        raise ValidationError(
            "%s agent %s must declare %s as an array-of-strings schema property"
            % (source, binding_name, property_name)
        )


def _normalize_output_schema(value: Dict[str, Any]) -> Dict[str, Any]:
    normalized = json.loads(json.dumps(value, allow_nan=False))

    def visit(candidate):
        if isinstance(candidate, dict):
            if candidate.get("type") == "object" and isinstance(candidate.get("properties"), dict):
                candidate.setdefault("additionalProperties", False)
            for child in candidate.values():
                visit(child)
        elif isinstance(candidate, list):
            for child in candidate:
                visit(child)

    visit(normalized)
    return normalized


def _agent_capture(binding_name: str, structured: bool = False) -> str:
    extension = "json" if structured else "md"
    return "claude-workflow/%s.%s" % (binding_name, extension)


def _contains_expression(value: Any) -> bool:
    if isinstance(value, (Reference, Template)):
        return True
    if isinstance(value, list):
        return any(_contains_expression(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_expression(item) for item in value.values())
    return False


def _diagnostic(text: str, source: Path, offset: int, message: str) -> str:
    line = text.count("\n", 0, offset) + 1
    prior_newline = text.rfind("\n", 0, offset)
    column = offset - prior_newline
    return "%s:%d:%d %s" % (source, line, column, message)
