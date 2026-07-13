"""Static compiler for a safe, useful subset of Claude saved workflows."""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .errors import ValidationError
from .packet_items import MAX_PACKET_ITEM_FILE_BYTES
from .workflow import MAX_CODEX_CONTEXT_SOURCES, SAFE_ID, SCHEMA


MAX_CLAUDE_WORKFLOW_BINDINGS = 64
MAX_CLAUDE_WORKFLOW_CONSTANTS = 64
MAX_CLAUDE_WORKFLOW_PHASES = 32
MAX_CLAUDE_WORKFLOW_DEPTH = 32
MAX_CLAUDE_WORKFLOW_TOKENS = 100000
MAX_CLAUDE_WORKFLOW_AGENTS = 1000
MAX_CLAUDE_WORKFLOW_WORKERS = 16
MAX_CLAUDE_WORKFLOW_RESULT_BYTES = 10 * 1024 * 1024
MAX_CLAUDE_WORKFLOW_JOIN_DELIMITER_CHARS = 128
MAX_CLAUDE_WORKFLOW_JOIN_DELIMITER_SOURCE_CHARS = 1024
CLAUDE_WORKFLOW_RESULT_ARTIFACT = "claude-workflow/result.json"
CLAUDE_AGENT_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_JS_REFERENCE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$"
)
_JS_REFERENCE_SOURCE = r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
_JS_TEMPLATE_FILTER = re.compile(
    r"^(?P<reference>%s)\.filter\(Boolean\)$" % _JS_REFERENCE_SOURCE
)
_JS_TEMPLATE_JOIN = re.compile(
    r"^(?P<reference>%s)(?P<filter>\.filter\(Boolean\))?\.join\((?P<delimiter>[\s\S]*)\)$"
    % _JS_REFERENCE_SOURCE
)
_JS_TEMPLATE_JSON = re.compile(
    r"^JSON\.stringify\(\s*(?P<reference>%s)(?P<filter>\.filter\(Boolean\))?\s*\)$"
    % _JS_REFERENCE_SOURCE
)
_JS_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)
_RESERVED_BINDINGS = {
    "agent",
    "args",
    "Boolean",
    "meta",
    "parallel",
    "phase",
    "pipeline",
}


@dataclass(frozen=True)
class Reference:
    parts: Tuple[str, ...]


@dataclass(frozen=True)
class RenderedMapReference:
    value: Reference
    filter_falsey: bool
    rendering: str


@dataclass(frozen=True)
class FilteredSource:
    value: Any


@dataclass(frozen=True)
class ArgAlias:
    name: str


@dataclass(frozen=True)
class Template:
    parts: Tuple[Union[str, Reference, RenderedMapReference], ...]


@dataclass(frozen=True)
class Concat:
    parts: Tuple[Any, ...]


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
class ParallelCall:
    source: Any
    item_name: str
    agent: AgentCall


@dataclass(frozen=True)
class MapHandoff:
    source_binding: str
    filter_falsey: bool
    step_id: str
    output: str


@dataclass(frozen=True)
class ConstantBinding:
    name: str
    value: Any


@dataclass(frozen=True)
class Binding:
    name: str
    expression: Union[AgentCall, PipelineCall, ParallelCall]
    phase: Optional[str] = None
    filters_falsey: bool = False


@dataclass(frozen=True)
class Program:
    meta: Dict[str, Any]
    constants: Tuple[ConstantBinding, ...]
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
            elif self.text.startswith("&&", self.index):
                self.tokens.append(Token("symbol", "&&", offset))
                self.index += 2
            elif self.text.startswith("=>", self.index):
                self.tokens.append(Token("symbol", "=>", offset))
                self.index += 2
            elif char in "{}[](),:;.=+":
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
        parts: List[Union[str, Reference, RenderedMapReference]] = []
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
                parts.append(
                    self._scan_template_interpolation(expression, expression_start)
                )
                self.index = expression_end + 1
                continue
            literal.append(char)
            self.index += 1
        self._fail(start, "has an unterminated template literal")

    def _scan_template_interpolation(
        self,
        expression: str,
        expression_start: int,
    ) -> Union[Reference, RenderedMapReference]:
        if _JS_REFERENCE.fullmatch(expression):
            return Reference(tuple(expression.split(".")))
        match = _JS_TEMPLATE_FILTER.fullmatch(expression)
        if match is not None:
            return RenderedMapReference(
                Reference(tuple(match.group("reference").split("."))),
                True,
                "value",
            )
        match = _JS_TEMPLATE_JOIN.fullmatch(expression)
        if match is not None:
            delimiter = match.group("delimiter").strip()
            if len(delimiter) > MAX_CLAUDE_WORKFLOW_JOIN_DELIMITER_SOURCE_CHARS:
                self._fail(
                    expression_start,
                    "template join() delimiter source is too long",
                )
            tokens = _Scanner(delimiter, self.source).scan()
            if len(tokens) != 2 or tokens[0].kind != "string":
                self._fail(
                    expression_start,
                    "template join() delimiter must be one static string",
                )
            if len(tokens[0].value) > MAX_CLAUDE_WORKFLOW_JOIN_DELIMITER_CHARS:
                self._fail(
                    expression_start,
                    "template join() delimiter is too long",
                )
            return RenderedMapReference(
                Reference(tuple(match.group("reference").split("."))),
                match.group("filter") is not None,
                "join",
            )
        match = _JS_TEMPLATE_JSON.fullmatch(expression)
        if match is not None:
            return RenderedMapReference(
                Reference(tuple(match.group("reference").split("."))),
                match.group("filter") is not None,
                "json",
            )
        self._fail(
            expression_start,
            "template interpolations may contain only dotted identifiers or exact map filter/join/JSON.stringify rendering",
        )

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
        constants: List[ConstantBinding] = []
        bindings: List[Binding] = []
        names = set()
        binding_names = set()
        return_name = None
        filters_falsey = False
        active_phase = None
        saw_binding = False
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
                declaration = self._parse_declaration(active_phase)
                if declaration.name in names:
                    self._fail("redeclares variable %s" % declaration.name)
                if declaration.name in _RESERVED_BINDINGS:
                    self._fail("must not bind reserved variable %s" % declaration.name)
                if not SAFE_ID.fullmatch(declaration.name):
                    self._fail("binding names must be safe Conductor identifiers")
                names.add(declaration.name)
                if isinstance(declaration, ConstantBinding):
                    if saw_binding:
                        self._fail("static constants must be declared before awaited bindings")
                    constants.append(declaration)
                    if len(constants) > MAX_CLAUDE_WORKFLOW_CONSTANTS:
                        self._fail(
                            "may define at most %d static constants"
                            % MAX_CLAUDE_WORKFLOW_CONSTANTS
                        )
                    continue
                saw_binding = True
                binding_names.add(declaration.name)
                bindings.append(declaration)
                if len(bindings) > MAX_CLAUDE_WORKFLOW_BINDINGS:
                    self._fail(
                        "may define at most %d agent, pipeline, or parallel bindings"
                        % MAX_CLAUDE_WORKFLOW_BINDINGS
                    )
                continue
            if self._at_identifier("phase"):
                active_phase = self._parse_phase_statement()
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
            self._fail("must define at least one awaited agent, pipeline, or parallel binding")
        if return_name is None:
            self._fail("must end with a return statement")
        if return_name not in binding_names:
            self._fail("returns unknown variable %s" % return_name)
        return Program(
            meta,
            tuple(constants),
            tuple(bindings),
            return_name,
            filters_falsey,
        )

    def _parse_meta(self) -> Dict[str, Any]:
        self._expect_identifier("export")
        self._expect_identifier("const")
        self._expect_identifier("meta")
        self._expect_symbol("=")
        value = self._parse_value()
        self._accept_symbol(";")
        try:
            value = _normalize_static_literal(value)
        except ValidationError:
            self._fail("export const meta must be a static object literal")
        if not isinstance(value, dict):
            self._fail("export const meta must be a static object literal")
        return value

    def _parse_declaration(
        self,
        active_phase: Optional[str],
    ) -> Union[ConstantBinding, Binding]:
        self._expect_identifier("const")
        name = self._expect_kind("identifier").value
        self._expect_symbol("=")
        wrapped = False
        if self._at_symbol("(") and self._peek_token().kind == "identifier" and self._peek_token().value == "await":
            self._expect_symbol("(")
            wrapped = True
        if not self._at_identifier("await"):
            value = self._parse_constant_value()
            self._accept_symbol(";")
            if isinstance(value, ArgAlias):
                return ConstantBinding(name, value)
            try:
                return ConstantBinding(name, _normalize_static_literal(value))
            except ValidationError:
                self._fail("static constants must contain only JSON-compatible literals")
        self._expect_identifier("await")
        call = self._expect_kind("identifier")
        if call.value == "agent":
            expression = self._parse_agent_call(call_consumed=True)
        elif call.value == "pipeline":
            expression = self._parse_pipeline_call()
        elif call.value == "parallel":
            expression = self._parse_parallel_call()
        else:
            self._fail("awaited calls may use only agent(), pipeline(), or parallel()", call)
        if wrapped:
            self._expect_symbol(")")
        filters_falsey = self._parse_optional_boolean_filter()
        self._accept_symbol(";")
        return Binding(name, expression, active_phase, filters_falsey)

    def _parse_constant_value(self) -> Any:
        if self._at_identifier("args") and self._peek_token().value == "&&":
            self._expect_identifier("args")
            self._expect_symbol("&&")
            reference = self._parse_reference()
            if len(reference.parts) != 2 or reference.parts[0] != "args":
                self._fail(
                    "guarded argument aliases must use exact `args && args.NAME` syntax"
                )
            return ArgAlias(reference.parts[1])
        value = self._parse_value()
        if (
            isinstance(value, Reference)
            and len(value.parts) == 2
            and value.parts[0] == "args"
        ):
            return ArgAlias(value.parts[1])
        return value

    def _parse_phase_statement(self) -> str:
        self._expect_identifier("phase")
        self._expect_symbol("(")
        title = self._expect_kind("string").value
        self._accept_symbol(",")
        self._expect_symbol(")")
        self._accept_symbol(";")
        if not title.strip():
            self._fail("phase title must be non-empty")
        return title

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
        source = self._parse_pipeline_source()
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

    def _parse_pipeline_source(self) -> Any:
        value = self._parse_value()
        if (
            isinstance(value, Reference)
            and len(value.parts) > 1
            and value.parts[-1] == "filter"
            and self._at_symbol("(")
        ):
            self._expect_symbol("(")
            self._expect_identifier("Boolean")
            self._expect_symbol(")")
            return FilteredSource(Reference(value.parts[:-1]))
        return value

    def _parse_parallel_call(self) -> ParallelCall:
        self._expect_symbol("(")
        source = self._parse_value()
        if isinstance(source, Reference) and source.parts[-1:] == ("map",):
            if len(source.parts) == 1:
                self._fail("parallel source must precede .map()")
            source = Reference(source.parts[:-1])
        else:
            self._expect_symbol(".")
            self._expect_identifier("map")
        self._expect_symbol("(")
        if self._accept_symbol("("):
            item_name = self._expect_kind("identifier").value
            self._expect_symbol(")")
        else:
            item_name = self._expect_kind("identifier").value
        if not _JS_IDENTIFIER.fullmatch(item_name):
            self._fail("parallel callback parameter must be an identifier")
        self._expect_symbol("=>")
        self._accept_identifier("async")
        self._expect_symbol("(")
        self._expect_symbol(")")
        self._expect_symbol("=>")
        self._accept_identifier("await")
        agent = self._parse_agent_call()
        self._accept_symbol(",")
        self._expect_symbol(")")
        self._accept_symbol(",")
        self._expect_symbol(")")
        return ParallelCall(source, item_name, agent)

    def _parse_return(self) -> Tuple[str, bool]:
        self._expect_identifier("return")
        name = self._expect_kind("identifier").value
        filters_falsey = self._parse_optional_boolean_filter()
        self._accept_symbol(";")
        return name, filters_falsey

    def _parse_optional_boolean_filter(self) -> bool:
        if not self._accept_symbol("."):
            return False
        self._expect_identifier("filter")
        self._expect_symbol("(")
        self._expect_identifier("Boolean")
        self._expect_symbol(")")
        return True

    def _parse_value(self, depth: int = 0) -> Any:
        value = self._parse_value_atom(depth)
        if not self._accept_symbol("+"):
            return value
        parts = list(value.parts) if isinstance(value, Concat) else [value]
        while True:
            part = self._parse_value_atom(depth)
            if isinstance(part, Concat):
                parts.extend(part.parts)
            else:
                parts.append(part)
            if not self._accept_symbol("+"):
                break
        return Concat(tuple(parts))

    def _parse_value_atom(self, depth: int = 0) -> Any:
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

    def _peek_token(self, distance: int = 1) -> Token:
        return self.tokens[min(self.index + distance, len(self.tokens) - 1)]

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


def _filtered_source(value: Any) -> Tuple[Any, bool]:
    if isinstance(value, FilteredSource):
        return value.value, True
    return value, False


def _map_handoff_item_semantics(
    owner_binding: Binding,
    structured_items: bool,
    source: Path,
    consumer_name: str,
    constants: Dict[str, Any],
) -> str:
    expression = owner_binding.expression
    if not isinstance(expression, (PipelineCall, ParallelCall)):
        raise ValidationError(
            "%s %s handoff source must be a prior map" % (source, consumer_name)
        )
    options = _agent_options(
        expression.agent.options,
        source,
        owner_binding.name,
        label_reference=expression.item_name,
        constants=constants,
    )
    schema = options.get("schema")
    if schema is None:
        if structured_items:
            raise ValidationError(
                "%s map %s must declare an object output schema before %s reads item properties"
                % (source, owner_binding.name, consumer_name)
            )
        return "opaque"
    schema_type = schema.get("type")
    if schema_type == "object":
        return "json"
    if schema_type == "string" and not structured_items:
        return "opaque"
    if structured_items:
        raise ValidationError(
            "%s map %s must declare an object output schema before %s reads item properties"
            % (source, owner_binding.name, consumer_name)
        )
    raise ValidationError(
        "%s map %s output schema must have root type string or object before it feeds %s"
        % (source, owner_binding.name, consumer_name)
    )


def _compile_program(program: Program, source: Path) -> Dict:
    command_name = program.meta.get("name")
    description = program.meta.get("description", "")
    if not isinstance(command_name, str) or not SAFE_ID.fullmatch(command_name):
        raise ValidationError("%s meta.name must be a safe non-empty identifier" % source)
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValidationError("%s meta.description must be a string when present" % source)
    when_to_use = program.meta.get("whenToUse", "")
    if when_to_use is None:
        when_to_use = ""
    if not isinstance(when_to_use, str):
        raise ValidationError("%s meta.whenToUse must be a string when present" % source)
    unsupported_meta = sorted(
        set(program.meta) - {"name", "description", "whenToUse", "phases"}
    )
    if unsupported_meta:
        raise ValidationError(
            "%s Claude-style meta contains unsupported field(s): %s"
            % (source, ", ".join(unsupported_meta))
        )

    constants = {binding.name: binding.value for binding in program.constants}
    phase_ids, phases_declared = _declared_phase_ids(program.meta.get("phases"), source)
    bindings = {binding.name: binding for binding in program.bindings}
    consumed_properties: Dict[str, Tuple[str, bool]] = {}
    handoffs: Dict[Tuple[str, bool], MapHandoff] = {}
    handoffs_by_owner: Dict[str, List[MapHandoff]] = {}

    def ensure_handoff(owner: str, filter_falsey: bool) -> MapHandoff:
        key = (owner, filter_falsey)
        handoff = handoffs.get(key)
        if handoff is None:
            index = len(handoffs) + 1
            handoff = MapHandoff(
                source_binding=owner,
                filter_falsey=filter_falsey,
                step_id="claude-handoff-%03d" % index,
                output="claude-workflow/handoffs/%03d.json" % index,
            )
            handoffs[key] = handoff
            handoffs_by_owner.setdefault(owner, []).append(handoff)
        return handoff

    direct_agents = sum(isinstance(binding.expression, AgentCall) for binding in program.bindings)
    map_calls = sum(
        isinstance(binding.expression, (PipelineCall, ParallelCall))
        for binding in program.bindings
    )
    if direct_agents >= MAX_CLAUDE_WORKFLOW_AGENTS:
        raise ValidationError("%s Claude-style workflow leaves no capacity for mapped agents" % source)
    per_map_items = (
        (MAX_CLAUDE_WORKFLOW_AGENTS - direct_agents) // map_calls if map_calls else 0
    )

    for binding in program.bindings:
        expression = binding.expression
        if binding.filters_falsey and not isinstance(expression, (PipelineCall, ParallelCall)):
            raise ValidationError(
                "%s filter(Boolean) may be used only on a pipeline or parallel result"
                % source
            )
        if isinstance(expression, AgentCall):
            context_sources = []
            for reference, reference_filters_falsey in _expression_context_references(
                expression.prompt
            ):
                if len(reference.parts) == 2 and reference.parts[0] == "args":
                    continue
                if reference.parts[0] in constants:
                    continue
                owner = reference.parts[0]
                owner_binding = bindings.get(owner)
                if (
                    len(reference.parts) == 1
                    and owner_binding is not None
                    and isinstance(owner_binding.expression, (PipelineCall, ParallelCall))
                ):
                    handoff = ensure_handoff(
                        owner,
                        reference_filters_falsey or owner_binding.filters_falsey,
                    )
                    if handoff.step_id not in context_sources:
                        context_sources.append(handoff.step_id)
            if len(context_sources) > MAX_CODEX_CONTEXT_SOURCES:
                raise ValidationError(
                    "%s %s agent prompt references more than %d map results"
                    % (source, binding.name, MAX_CODEX_CONTEXT_SOURCES)
                )
        if not isinstance(expression, (PipelineCall, ParallelCall)):
            continue
        reference, source_filters_falsey = _filtered_source(expression.source)
        if isinstance(reference, Reference) and len(reference.parts) == 1:
            if reference.parts[0] in constants:
                if source_filters_falsey:
                    raise ValidationError(
                        "%s mapped binding %s may filter only a prior map result"
                        % (source, binding.name)
                    )
                continue
            owner = reference.parts[0]
            owner_binding = bindings.get(owner)
            if owner_binding is not None and isinstance(
                owner_binding.expression,
                (PipelineCall, ParallelCall),
            ):
                _, structured_items = _pipeline_prompt(
                    expression.agent.prompt,
                    expression.item_name,
                    source,
                    binding.name,
                    constants=constants,
                )
                _map_handoff_item_semantics(
                    owner_binding,
                    structured_items,
                    source,
                    binding.name,
                    constants,
                )
                filter_falsey = source_filters_falsey or owner_binding.filters_falsey
                ensure_handoff(owner, filter_falsey)
                continue
            if source_filters_falsey:
                raise ValidationError(
                    "%s mapped binding %s may filter only a prior map result"
                    % (source, binding.name)
                )
        if isinstance(reference, Reference) and len(reference.parts) == 2:
            if source_filters_falsey:
                raise ValidationError(
                    "%s mapped binding %s may filter only a prior map result"
                    % (source, binding.name)
                )
            owner, property_name = reference.parts
            if owner == "args":
                continue
            owner_binding = bindings.get(owner)
            if owner_binding is None or not isinstance(owner_binding.expression, AgentCall):
                raise ValidationError(
                    "%s mapped binding %s source must reference args, a static constant, or a prior agent result"
                    % (source, binding.name)
                )
            _, structured_items = _pipeline_prompt(
                expression.agent.prompt,
                expression.item_name,
                source,
                binding.name,
                constants=constants,
            )
            previous = consumed_properties.get(owner)
            if previous is not None and previous != (property_name, structured_items):
                raise ValidationError(
                    "%s agent result %s cannot feed multiple schema properties" % (source, owner)
                )
            consumed_properties[owner] = (property_name, structured_items)

    consumed_maps = {owner for owner, _ in handoffs}
    for binding in program.bindings:
        if (
            binding.filters_falsey
            and binding.name != program.return_name
            and binding.name not in consumed_maps
        ):
            raise ValidationError(
                "%s filter(Boolean) on %s cannot feed a later binding"
                % (source, binding.name)
            )

    steps = []
    prior_step = None
    seen_bindings = set()
    for binding in program.bindings:
        expression = binding.expression
        dependencies = [prior_step] if prior_step is not None else []
        if isinstance(expression, AgentCall):
            options = _agent_options(
                expression.options,
                source,
                binding.name,
                constants=constants,
            )
            prompt, context_sources = _agent_prompt(
                expression.prompt,
                source,
                binding.name,
                constants=constants,
                bindings=bindings,
                seen_bindings=seen_bindings,
                handoffs=handoffs,
            )
            consumed_property = consumed_properties.get(binding.name)
            if consumed_property is not None:
                property_name, structured_items = consumed_property
                _validate_array_schema(
                    options.get("schema"),
                    property_name,
                    source,
                    binding.name,
                    structured_items=structured_items,
                )
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
            if options.get("effort") is not None:
                step["effort"] = options["effort"]
            if context_sources:
                step["context_from"] = context_sources
                for context_source in context_sources:
                    if context_source not in dependencies:
                        dependencies.append(context_source)
        else:
            if per_map_items < 1:
                raise ValidationError("%s Claude-style workflow exceeds its 1,000-agent cap" % source)
            options = _agent_options(
                expression.agent.options,
                source,
                binding.name,
                label_reference=expression.item_name,
                constants=constants,
            )
            if isinstance(expression, PipelineCall):
                prompt_template, structured_items = _pipeline_prompt(
                    expression.agent.prompt,
                    expression.item_name,
                    source,
                    binding.name,
                    constants=constants,
                )
                item_source, source_dependency = _pipeline_source(
                    expression.source,
                    binding.name,
                    bindings,
                    seen_bindings,
                    per_map_items,
                    source,
                    constants=constants,
                    structured_items=structured_items,
                    handoffs=handoffs,
                )
                if structured_items:
                    item_source["item_semantics"] = "json"
            else:
                prompt_template, item_source, source_dependency = _parallel_map(
                    expression,
                    binding.name,
                    bindings,
                    seen_bindings,
                    per_map_items,
                    source,
                    constants,
                    handoffs,
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
                "max_items": per_map_items,
                "max_workers": MAX_CLAUDE_WORKFLOW_WORKERS,
                "preserve_duplicate_items": True,
            }
            if options.get("schema") is not None:
                step["output_schema"] = _normalize_output_schema(options["schema"])
            if options.get("effort") is not None:
                step["effort"] = options["effort"]
            step.update(item_source)
        phase_name = _effective_phase_name(binding, options.get("phase"), source)
        if phase_name is not None:
            step["phase"] = _resolve_phase_id(
                phase_name,
                phase_ids,
                phases_declared,
                source,
            )
        if dependencies:
            step["depends_on"] = dependencies
        steps.append(step)
        seen_bindings.add(binding.name)
        prior_step = binding.name
        for handoff in handoffs_by_owner.get(binding.name, []):
            handoff_step = {
                "id": handoff.step_id,
                "kind": "collect_results",
                "risk": "low",
                "source_step": binding.name,
                "output": handoff.output,
                "output_limit_bytes": MAX_PACKET_ITEM_FILE_BYTES,
                "depends_on": [binding.name],
                "intermediate": True,
            }
            if handoff.filter_falsey:
                handoff_step["filter_falsey"] = True
            steps.append(handoff_step)
            prior_step = handoff.step_id

    returned = bindings[program.return_name].expression
    filters_falsey = program.filters_falsey or bindings[program.return_name].filters_falsey
    if filters_falsey and not isinstance(returned, (PipelineCall, ParallelCall)):
        raise ValidationError(
            "%s filter(Boolean) may be used only on a pipeline or parallel result" % source
        )
    result_step = {
        "id": "claude-result",
        "kind": "collect_results",
        "risk": "low",
        "source_step": program.return_name,
        "output": CLAUDE_WORKFLOW_RESULT_ARTIFACT,
        "output_limit_bytes": MAX_CLAUDE_WORKFLOW_RESULT_BYTES,
        "depends_on": [program.return_name],
    }
    if filters_falsey:
        result_step["filter_falsey"] = True
    steps.append(result_step)
    return {
        "schema": SCHEMA,
        "name": command_name,
        "description": description,
        "mode": "read_only",
        "result_artifact": CLAUDE_WORKFLOW_RESULT_ARTIFACT,
        "max_workers": MAX_CLAUDE_WORKFLOW_WORKERS if map_calls else 1,
        "max_items": per_map_items if map_calls else MAX_CLAUDE_WORKFLOW_AGENTS,
        "steps": steps,
    }


def _agent_options(
    options: Dict[str, Any],
    source: Path,
    binding_name: str,
    label_reference: Optional[str] = None,
    constants: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    constants = constants or {}
    unsupported = sorted(set(options) - {"label", "phase", "schema", "effort"})
    if unsupported:
        raise ValidationError(
            "%s %s agent options contain unsupported field(s): %s"
            % (source, binding_name, ", ".join(unsupported))
        )
    if "label" in options:
        label = options["label"]
        if not isinstance(label, (str, Template, Concat, Reference)):
            raise ValidationError("%s %s agent label must be static text or a reference" % (source, binding_name))
        if any(
            not isinstance(part, (str, Reference))
            for part in _text_expression_parts(label)
        ):
            raise ValidationError(
                "%s %s agent label text may contain only text and references"
                % (source, binding_name)
            )
        references = _expression_references(label)
        for reference in references:
            if label_reference is not None and reference.parts[0] == label_reference:
                continue
            if len(reference.parts) == 2 and reference.parts[0] == "args":
                continue
            if reference.parts[0] in constants:
                _resolve_static_reference(reference, constants, source, binding_name)
                continue
            raise ValidationError(
                "%s %s agent label may reference only args.NAME%s"
                % (
                    source,
                    binding_name,
                    " or %s" % label_reference if label_reference is not None else "",
                )
            )
    normalized = dict(options)
    if "schema" in options:
        schema = options["schema"]
        if isinstance(schema, Reference):
            schema = _resolve_static_reference(schema, constants, source, binding_name)
        if not isinstance(schema, dict) or _contains_expression(schema):
            raise ValidationError(
                "%s %s agent schema must be a static object literal or constant"
                % (source, binding_name)
            )
        normalized["schema"] = json.loads(json.dumps(schema, allow_nan=False))
    if "phase" in options:
        phase = options["phase"]
        if isinstance(phase, Reference):
            phase = _resolve_static_reference(phase, constants, source, binding_name)
        if not isinstance(phase, str) or not phase.strip():
            raise ValidationError(
                "%s %s agent phase must be static non-empty text"
                % (source, binding_name)
            )
        normalized["phase"] = phase
    if "effort" in options:
        effort = options["effort"]
        if isinstance(effort, Reference):
            effort = _resolve_static_reference(effort, constants, source, binding_name)
        if not isinstance(effort, str) or effort not in CLAUDE_AGENT_EFFORTS:
            raise ValidationError(
                "%s %s agent effort must be low, medium, high, xhigh, max, or ultra"
                % (source, binding_name)
            )
        normalized["effort"] = "ultra" if effort == "max" else effort
    return normalized


def _agent_prompt(
    value: Any,
    source: Path,
    binding_name: str,
    constants: Optional[Dict[str, Any]] = None,
    bindings: Optional[Dict[str, Binding]] = None,
    seen_bindings: Optional[set] = None,
    handoffs: Optional[Dict[Tuple[str, bool], MapHandoff]] = None,
) -> Tuple[str, List[str]]:
    constants = constants or {}
    bindings = bindings or {}
    seen_bindings = seen_bindings or set()
    handoffs = handoffs or {}
    if not isinstance(value, (str, Reference, Template, Concat)):
        raise ValidationError(
            "%s %s agent prompt must be text, args.NAME, or a text constant"
            % (source, binding_name)
        )
    output = []
    context_sources = []
    allow_map_context = isinstance(value, (Template, Concat))

    def append_map_context(
        reference: Reference,
        *,
        filter_falsey: bool,
        rendering: str,
    ) -> None:
        owner = reference.parts[0]
        owner_binding = bindings.get(owner)
        if len(reference.parts) != 1 or owner_binding is None or not isinstance(
            owner_binding.expression,
            (PipelineCall, ParallelCall),
        ):
            raise ValidationError(
                "%s %s agent prompt references unknown map %s"
                % (source, binding_name, ".".join(reference.parts))
            )
        if owner not in seen_bindings:
            raise ValidationError(
                "%s %s agent prompt must reference a prior map"
                % (source, binding_name)
            )
        effective_filter = filter_falsey or owner_binding.filters_falsey
        handoff = handoffs.get((owner, effective_filter))
        if handoff is None:
            raise ValidationError(
                "%s %s agent prompt has no compiled handoff for map %s"
                % (source, binding_name, owner)
            )
        output.append(
            "[The %s value of %s is provided below as completed dependency evidence from step %s.]"
            % (rendering, owner, handoff.step_id)
        )
        if handoff.step_id not in context_sources:
            context_sources.append(handoff.step_id)

    for part in _text_expression_parts(value):
        if isinstance(part, str):
            output.append(part)
        elif isinstance(part, RenderedMapReference) and allow_map_context:
            append_map_context(
                part.value,
                filter_falsey=part.filter_falsey,
                rendering=part.rendering,
            )
        elif (
            isinstance(part, Reference)
            and len(part.parts) == 2
            and part.parts[0] == "args"
        ):
            output.append("{{args.%s}}" % part.parts[1])
        elif isinstance(part, Reference) and part.parts[0] in constants:
            output.append(
                _static_js_text(
                    _resolve_static_reference(part, constants, source, binding_name),
                    source,
                    binding_name,
                )
            )
        elif isinstance(part, Reference) and len(part.parts) == 1 and allow_map_context:
            append_map_context(
                part,
                filter_falsey=False,
                rendering="value",
            )
        else:
            raise ValidationError(
                "%s %s agent prompt text may reference only args.NAME, static constants, or a prior map result"
                % (source, binding_name)
            )
    return "".join(output), context_sources


def _pipeline_prompt(
    value: Any,
    item_name: str,
    source: Path,
    binding_name: str,
    constants: Optional[Dict[str, Any]] = None,
) -> Tuple[str, bool]:
    constants = constants or {}
    if not isinstance(value, (Template, Concat)):
        raise ValidationError(
            "%s %s pipeline agent prompt must use a template or text concatenation containing %s"
            % (source, binding_name, item_name)
        )
    output = []
    item_references = 0
    structured_items = False
    for part in _text_expression_parts(value):
        if isinstance(part, str):
            output.append(part.replace("{", "{{").replace("}", "}}"))
        elif isinstance(part, Reference) and part.parts == (item_name,):
            output.append("{item}")
            item_references += 1
        elif isinstance(part, Reference) and part.parts[0] == item_name:
            output.append("{item.%s}" % ".".join(part.parts[1:]))
            item_references += 1
            structured_items = True
        elif (
            isinstance(part, Reference)
            and len(part.parts) == 2
            and part.parts[0] == "args"
        ):
            output.append("{{args.%s}}" % part.parts[1])
        elif isinstance(part, Reference) and part.parts[0] in constants:
            resolved = _resolve_static_reference(
                part,
                constants,
                source,
                binding_name,
            )
            text = _static_js_text(
                resolved,
                source,
                binding_name,
            )
            output.append(text if isinstance(resolved, ArgAlias) else _escape_format_text(text))
        else:
            raise ValidationError(
                "%s %s pipeline prompt may interpolate only its %s callback parameter, args.NAME, or static constants"
                % (source, binding_name, item_name)
            )
    if item_references == 0:
        raise ValidationError(
            "%s %s pipeline prompt must interpolate its %s callback parameter"
            % (source, binding_name, item_name)
        )
    return "".join(output), structured_items


def _parallel_map(
    expression: ParallelCall,
    binding_name: str,
    bindings: Dict[str, Binding],
    seen_bindings: set,
    max_items: int,
    source: Path,
    constants: Dict[str, Any],
    handoffs: Dict[Tuple[str, bool], MapHandoff],
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    static_items = None
    if isinstance(expression.source, list):
        static_items = expression.source
    elif (
        isinstance(expression.source, Reference)
        and len(expression.source.parts) == 1
        and expression.source.parts[0] in constants
    ):
        candidate = constants[expression.source.parts[0]]
        if not isinstance(candidate, ArgAlias):
            static_items = candidate

    if static_items is not None:
        if not isinstance(static_items, list) or not static_items:
            raise ValidationError(
                "%s %s parallel static source must be a non-empty array"
                % (source, binding_name)
            )
        if len(static_items) > max_items:
            raise ValidationError(
                "%s %s parallel static source exceeds its agent budget"
                % (source, binding_name)
            )
        prompts = [
            _parallel_static_prompt(
                expression.agent.prompt,
                expression.item_name,
                item,
                constants,
                source,
                binding_name,
            )
            for item in static_items
        ]
        return (
            "{item}",
            {"items": prompts, "item_semantics": "opaque"},
            None,
        )

    prompt_template, structured_items = _pipeline_prompt(
        expression.agent.prompt,
        expression.item_name,
        source,
        binding_name,
        constants=constants,
    )
    item_source, dependency = _pipeline_source(
        expression.source,
        binding_name,
        bindings,
        seen_bindings,
        max_items,
        source,
        constants=constants,
        structured_items=structured_items,
        handoffs=handoffs,
    )
    if structured_items:
        item_source["item_semantics"] = "json"
    elif "items" in item_source:
        item_source["item_semantics"] = "opaque"
    return prompt_template, item_source, dependency


def _parallel_static_prompt(
    value: Any,
    item_name: str,
    item: Any,
    constants: Dict[str, Any],
    source: Path,
    binding_name: str,
) -> str:
    if isinstance(value, str):
        output = value
    elif isinstance(value, Reference):
        output = _static_js_text(
            _parallel_reference_value(
                value,
                item_name,
                item,
                constants,
                source,
                binding_name,
            ),
            source,
            binding_name,
        )
    elif isinstance(value, (Template, Concat)):
        parts = []
        for part in _text_expression_parts(value):
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Reference):
                parts.append(
                    _static_js_text(
                        _parallel_reference_value(
                            part,
                            item_name,
                            item,
                            constants,
                            source,
                            binding_name,
                        ),
                        source,
                        binding_name,
                    )
                )
            else:
                raise ValidationError(
                    "%s %s parallel agent prompt text may contain only text and references"
                    % (source, binding_name)
                )
        output = "".join(parts)
    else:
        raise ValidationError(
            "%s %s parallel agent prompt must resolve to text"
            % (source, binding_name)
        )
    if not output.strip():
        raise ValidationError(
            "%s %s parallel agent prompt must resolve to non-empty text"
            % (source, binding_name)
        )
    return output


def _parallel_reference_value(
    reference: Reference,
    item_name: str,
    item: Any,
    constants: Dict[str, Any],
    source: Path,
    binding_name: str,
) -> Any:
    if reference.parts[0] == item_name:
        current = item
        for part in reference.parts[1:]:
            if not isinstance(current, dict) or part not in current:
                raise ValidationError(
                    "%s %s parallel item has no static property %s"
                    % (source, binding_name, ".".join(reference.parts[1:]))
                )
            current = current[part]
        return current
    if len(reference.parts) == 2 and reference.parts[0] == "args":
        return "{{args.%s}}" % reference.parts[1]
    if reference.parts[0] in constants:
        return _resolve_static_reference(reference, constants, source, binding_name)
    raise ValidationError(
        "%s %s parallel prompt may reference only %s, args.NAME, or static constants"
        % (source, binding_name, item_name)
    )


def _pipeline_source(
    value: Any,
    binding_name: str,
    bindings: Dict[str, Binding],
    seen_bindings: set,
    max_items: int,
    source: Path,
    constants: Optional[Dict[str, Any]] = None,
    structured_items: bool = False,
    handoffs: Optional[Dict[Tuple[str, bool], MapHandoff]] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    constants = constants or {}
    handoffs = handoffs or {}
    value, source_filters_falsey = _filtered_source(value)
    if isinstance(value, Reference) and len(value.parts) == 1 and value.parts[0] in constants:
        resolved = constants[value.parts[0]]
        value = (
            Reference(("args", resolved.name))
            if isinstance(resolved, ArgAlias)
            else resolved
        )
    if isinstance(value, Reference) and len(value.parts) == 1:
        owner = value.parts[0]
        owner_binding = bindings.get(owner)
        if owner_binding is not None and isinstance(
            owner_binding.expression,
            (PipelineCall, ParallelCall),
        ):
            if owner not in seen_bindings:
                raise ValidationError(
                    "%s %s pipeline source must reference a prior map"
                    % (source, binding_name)
                )
            filter_falsey = source_filters_falsey or owner_binding.filters_falsey
            handoff = handoffs.get((owner, filter_falsey))
            if handoff is None:
                raise ValidationError(
                    "%s %s pipeline source has no compiled handoff for map %s"
                    % (source, binding_name, owner)
                )
            item_semantics = _map_handoff_item_semantics(
                owner_binding,
                structured_items,
                source,
                binding_name,
                constants,
            )
            return {
                "items_artifact": handoff.output,
                "items_pointer": "",
                "item_semantics": item_semantics,
            }, handoff.step_id
    if source_filters_falsey:
        raise ValidationError(
            "%s %s may filter only a prior map result" % (source, binding_name)
        )
    if isinstance(value, list):
        valid_items = (
            all(isinstance(item, dict) and item for item in value)
            if structured_items
            else all(isinstance(item, str) and item for item in value)
        )
        if not value or not valid_items:
            item_type = "object" if structured_items else "string"
            raise ValidationError(
                "%s %s pipeline literal source must be a non-empty %s array"
                % (source, binding_name, item_type)
            )
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
    schema = owner_binding.expression.options.get("schema")
    if isinstance(schema, Reference):
        schema = _resolve_static_reference(schema, constants, source, owner)
    _validate_array_schema(
        schema,
        property_name,
        source,
        owner,
        structured_items=structured_items,
    )
    pointer = "/" + property_name.replace("~", "~0").replace("/", "~1")
    return {
        "items_artifact": _agent_capture(owner, structured=True),
        "items_pointer": pointer,
    }, owner


def _validate_string_array_schema(schema: Any, property_name: str, source: Path, binding_name: str) -> None:
    _validate_array_schema(
        schema,
        property_name,
        source,
        binding_name,
        structured_items=False,
    )


def _validate_array_schema(
    schema: Any,
    property_name: str,
    source: Path,
    binding_name: str,
    *,
    structured_items: bool,
) -> None:
    valid = isinstance(schema, dict) and schema.get("type") == "object"
    properties = schema.get("properties") if valid else None
    property_schema = properties.get(property_name) if isinstance(properties, dict) else None
    valid = (
        valid
        and isinstance(property_schema, dict)
        and property_schema.get("type") == "array"
        and isinstance(property_schema.get("items"), dict)
        and property_schema["items"].get("type")
        == ("object" if structured_items else "string")
    )
    required = schema.get("required") if isinstance(schema, dict) else None
    valid = valid and isinstance(required, list) and property_name in required
    if not valid:
        item_type = "objects" if structured_items else "strings"
        raise ValidationError(
            "%s agent %s must declare %s as an array-of-%s schema property"
            % (source, binding_name, property_name, item_type)
        )


def _declared_phase_ids(value: Any, source: Path) -> Tuple[Dict[str, str], bool]:
    if value is None:
        return {}, False
    if not isinstance(value, list) or not value or len(value) > MAX_CLAUDE_WORKFLOW_PHASES:
        raise ValidationError(
            "%s meta.phases must be a non-empty array of at most %d phases"
            % (source, MAX_CLAUDE_WORKFLOW_PHASES)
        )
    result: Dict[str, str] = {}
    ids = set()
    for index, phase in enumerate(value, start=1):
        if not isinstance(phase, dict) or set(phase) - {"title", "detail"}:
            raise ValidationError(
                "%s meta.phases[%d] must contain only title and optional detail"
                % (source, index - 1)
            )
        title = phase.get("title")
        detail = phase.get("detail", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 128:
            raise ValidationError(
                "%s meta.phases[%d].title must be non-empty text of at most 128 characters"
                % (source, index - 1)
            )
        if not isinstance(detail, str) or len(detail) > 1024:
            raise ValidationError(
                "%s meta.phases[%d].detail must be text of at most 1024 characters"
                % (source, index - 1)
            )
        if title in result:
            raise ValidationError("%s meta.phases repeats title %s" % (source, title))
        phase_id = _phase_identifier(title, source)
        if phase_id in ids:
            raise ValidationError(
                "%s meta.phases titles collapse to the same Conductor phase id %s"
                % (source, phase_id)
            )
        result[title] = phase_id
        ids.add(phase_id)
    return result, True


def _effective_phase_name(
    binding: Binding,
    option_phase: Optional[str],
    source: Path,
) -> Optional[str]:
    if binding.phase is not None and option_phase is not None and binding.phase != option_phase:
        raise ValidationError(
            "%s %s phase marker %r conflicts with agent option phase %r"
            % (source, binding.name, binding.phase, option_phase)
        )
    return option_phase if option_phase is not None else binding.phase


def _resolve_phase_id(
    title: str,
    phase_ids: Dict[str, str],
    phases_declared: bool,
    source: Path,
) -> str:
    if title in phase_ids:
        return phase_ids[title]
    if phases_declared:
        raise ValidationError(
            "%s phase %r is not declared in meta.phases" % (source, title)
        )
    phase_id = _phase_identifier(title, source)
    if phase_id in phase_ids.values():
        raise ValidationError(
            "%s phase titles collapse to the same Conductor phase id %s"
            % (source, phase_id)
        )
    phase_ids[title] = phase_id
    return phase_id


def _phase_identifier(title: str, source: Path) -> str:
    if SAFE_ID.fullmatch(title):
        return title
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", title).strip("-.")
    if not value or not SAFE_ID.fullmatch(value):
        raise ValidationError("%s phase title %r cannot form a safe identifier" % (source, title))
    return value


def _normalize_static_literal(value: Any) -> Any:
    if isinstance(value, ArgAlias):
        raise ValidationError("literal contains an argument alias")
    if isinstance(value, Concat):
        normalized = [_normalize_static_literal(part) for part in value.parts]
        if not normalized or not all(isinstance(part, str) for part in normalized):
            raise ValidationError("static concatenation must contain only text")
        return "".join(normalized)
    if isinstance(value, Template):
        if any(not isinstance(part, str) for part in value.parts):
            raise ValidationError("template contains an interpolation")
        return "".join(value.parts)
    if isinstance(value, Reference):
        raise ValidationError("literal contains a reference")
    if isinstance(value, list):
        return [_normalize_static_literal(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_static_literal(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValidationError("unsupported static literal")


def _resolve_static_reference(
    reference: Reference,
    constants: Dict[str, Any],
    source: Path,
    binding_name: str,
) -> Any:
    if not reference.parts or reference.parts[0] not in constants:
        raise ValidationError(
            "%s %s references unknown static constant %s"
            % (source, binding_name, reference.parts[0] if reference.parts else "")
        )
    current = constants[reference.parts[0]]
    if isinstance(current, ArgAlias) and len(reference.parts) > 1:
        raise ValidationError(
            "%s %s argument alias %s cannot be dereferenced as a static object"
            % (source, binding_name, reference.parts[0])
        )
    for part in reference.parts[1:]:
        if not isinstance(current, dict) or part not in current:
            raise ValidationError(
                "%s %s static constant has no property %s"
                % (source, binding_name, ".".join(reference.parts[1:]))
            )
        current = current[part]
    return current


def _static_js_text(value: Any, source: Path, binding_name: str) -> str:
    if isinstance(value, ArgAlias):
        return "{{args.%s}}" % value.name
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    raise ValidationError(
        "%s %s prompt interpolation must resolve to a scalar"
        % (source, binding_name)
    )


def _escape_format_text(value: str) -> str:
    return value.replace("{", "{{").replace("}", "}}")


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
    if isinstance(value, (Reference, RenderedMapReference, Template, Concat)):
        return True
    if isinstance(value, list):
        return any(_contains_expression(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_expression(item) for item in value.values())
    return False


def _text_expression_parts(value: Any) -> List[Any]:
    if isinstance(value, Template):
        return list(value.parts)
    if isinstance(value, Concat):
        parts: List[Any] = []
        for part in value.parts:
            parts.extend(_text_expression_parts(part))
        return parts
    return [value]


def _expression_references(value: Any) -> List[Reference]:
    return [reference for reference, _ in _expression_context_references(value)]


def _expression_context_references(value: Any) -> List[Tuple[Reference, bool]]:
    references = []
    for part in _text_expression_parts(value):
        if isinstance(part, Reference):
            references.append((part, False))
        elif isinstance(part, RenderedMapReference):
            references.append((part.value, part.filter_falsey))
    return references


def _diagnostic(text: str, source: Path, offset: int, message: str) -> str:
    line = text.count("\n", 0, offset) + 1
    prior_newline = text.rfind("\n", 0, offset)
    column = offset - prior_newline
    return "%s:%d:%d %s" % (source, line, column, message)
