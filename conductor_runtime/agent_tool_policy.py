import base64
import hashlib
import json
import re
import shlex
from typing import Dict, List, Optional, Tuple

from .errors import ValidationError


AGENT_COMMAND_POLICY_SCHEMA_V1 = "conductor.agent_command_policy.v1"
AGENT_COMMAND_POLICY_SCHEMA_V2 = "conductor.agent_command_policy.v2"
AGENT_COMMAND_POLICY_SCHEMA = AGENT_COMMAND_POLICY_SCHEMA_V1
COMMAND_RULE_FIELDS = {"argv", "argv_prefix"}
MAX_COMMAND_RULES = 64
MAX_COMMAND_ARGV = 64
MAX_COMMAND_ARG_CHARS = 2048
MAX_COMMAND_POLICY_BYTES = 128 * 1024
MAX_HOOK_INPUT_BYTES = 1024 * 1024
MAX_NATIVE_TOOL_RULES = 64
MAX_NATIVE_TOOL_NAME_CHARS = 256
SAFE_NATIVE_MCP_TOOL = re.compile(
    r"^mcp__[A-Za-z0-9][A-Za-z0-9_-]{0,63}__[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_UNQUOTED_SHELL_SYNTAX = set(";&|<>()`$*?[]{}~!#")


def validate_command_allowlist(value, source: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_COMMAND_RULES:
        raise ValidationError(
            "%s must be an array of at most %d command rules"
            % (source, MAX_COMMAND_RULES)
        )
    seen = set()
    for index, rule in enumerate(value):
        label = "%s[%d]" % (source, index)
        if not isinstance(rule, dict) or len(rule) != 1 or not set(rule) <= COMMAND_RULE_FIELDS:
            raise ValidationError(
                "%s must contain exactly one of argv or argv_prefix" % label
            )
        field = next(iter(rule))
        argv = rule[field]
        if not isinstance(argv, list) or not argv or len(argv) > MAX_COMMAND_ARGV:
            raise ValidationError(
                "%s %s must be a non-empty array of at most %d arguments"
                % (label, field, MAX_COMMAND_ARGV)
            )
        for arg_index, arg in enumerate(argv):
            if (
                not isinstance(arg, str)
                or not arg
                or len(arg) > MAX_COMMAND_ARG_CHARS
                or "\x00" in arg
                or "\r" in arg
                or "\n" in arg
                or any(ord(char) < 32 and char != "\t" for char in arg)
            ):
                raise ValidationError(
                    "%s %s[%d] must be a bounded non-empty single-line string"
                    % (label, field, arg_index)
                )
        identity = (field, tuple(argv))
        if identity in seen:
            raise ValidationError("%s contains a duplicate command rule" % source)
        seen.add(identity)
    policy = {
        "schema": AGENT_COMMAND_POLICY_SCHEMA,
        "command_allowlist": value,
        "allow_apply_patch": False,
    }
    if len(_canonical_policy_bytes(policy)) > MAX_COMMAND_POLICY_BYTES:
        raise ValidationError(
            "%s produces a policy larger than %d bytes"
            % (source, MAX_COMMAND_POLICY_BYTES)
        )


def effective_command_policy(
    profile: Dict,
    additional_rules: Optional[List[Dict]] = None,
    additional_tools: Optional[List[str]] = None,
) -> Dict:
    rules = list(profile.get("command_allowlist", [])) + list(additional_rules or [])
    validate_command_allowlist(rules, "agent profile command_allowlist")
    allow_patch = profile.get("allow_apply_patch", False)
    if not isinstance(allow_patch, bool):
        raise ValidationError("agent profile allow_apply_patch must be a boolean")
    tools = list(additional_tools or [])
    validate_native_tool_allowlist(tools, "agent profile native tool allowlist")
    policy = {
        "schema": (
            AGENT_COMMAND_POLICY_SCHEMA_V2
            if tools
            else AGENT_COMMAND_POLICY_SCHEMA_V1
        ),
        "command_allowlist": json.loads(json.dumps(rules)),
        "allow_apply_patch": allow_patch,
    }
    if tools:
        policy["tool_allowlist"] = list(tools)
    if len(_canonical_policy_bytes(policy)) > MAX_COMMAND_POLICY_BYTES:
        raise ValidationError("agent command policy exceeds the byte limit")
    return policy


def command_policy_sha256(policy: Dict) -> str:
    _validate_effective_policy(policy)
    return hashlib.sha256(_canonical_policy_bytes(policy)).hexdigest()


def evaluate_pre_tool_use(policy: Dict, hook_input: Dict) -> Tuple[bool, str]:
    _validate_effective_policy(policy)
    if not isinstance(hook_input, dict) or hook_input.get("hook_event_name") != "PreToolUse":
        return False, "Conductor command policy received invalid hook input."
    tool_name = hook_input.get("tool_name")
    if tool_name == "apply_patch":
        if policy["allow_apply_patch"]:
            return True, ""
        return False, "Conductor profile does not allow apply_patch."
    if tool_name in policy.get("tool_allowlist", []):
        return True, ""
    if tool_name != "Bash":
        return False, "Conductor profile does not allow this tool."
    tool_input = hook_input.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False, "Conductor command policy requires a string Bash command."
    try:
        argv = parse_simple_shell_command(command)
    except ValidationError:
        return False, "Conductor profile allows only one expansion-free simple command."
    for rule in policy["command_allowlist"]:
        if "argv" in rule and argv == rule["argv"]:
            return True, ""
        prefix = rule.get("argv_prefix")
        if prefix is not None and argv[: len(prefix)] == prefix:
            return True, ""
    return False, "Bash command is outside the Conductor profile allowlist."


def parse_simple_shell_command(command: str) -> List[str]:
    if not isinstance(command, str) or not command or len(command.encode("utf-8")) > 64 * 1024:
        raise ValidationError("command must be a bounded non-empty string")
    quote = None
    escaped = False
    for char in command:
        if char in "\r\n\x00" or (ord(char) < 32 and char not in "\t"):
            raise ValidationError("command contains a control character")
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "\\":
                escaped = True
            elif char in {"$", "`"}:
                raise ValidationError("command contains expansion syntax")
            continue
        if char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            quote = char
        elif char in _UNQUOTED_SHELL_SYNTAX:
            raise ValidationError("command contains shell syntax")
    if quote is not None or escaped:
        raise ValidationError("command contains incomplete quoting")
    try:
        argv = shlex.split(command, comments=False, posix=True)
    except ValueError as exc:
        raise ValidationError("command quoting is invalid") from exc
    if not argv or _ASSIGNMENT.match(argv[0]):
        raise ValidationError("command has no direct executable")
    return argv


def hook_denial(reason: str) -> Dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def build_pre_tool_hook_command(policy: Dict, python_executable: str) -> str:
    _validate_effective_policy(policy)
    payload = _canonical_policy_bytes(policy)
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    return shlex.join(
        [
            python_executable,
            "-I",
            "-c",
            _HOOK_PROGRAM,
            encoded,
            digest,
        ]
    )


def _canonical_policy_bytes(policy: Dict) -> bytes:
    return json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_effective_policy(policy: Dict) -> None:
    if not isinstance(policy, dict):
        raise ValidationError("agent command policy has invalid fields")
    schema = policy.get("schema")
    expected = {"schema", "command_allowlist", "allow_apply_patch"}
    if schema == AGENT_COMMAND_POLICY_SCHEMA_V2:
        expected.add("tool_allowlist")
    elif schema != AGENT_COMMAND_POLICY_SCHEMA_V1:
        raise ValidationError("agent command policy schema is invalid")
    if set(policy) != expected:
        raise ValidationError("agent command policy has invalid fields")
    validate_command_allowlist(policy.get("command_allowlist"), "agent command policy allowlist")
    if not isinstance(policy.get("allow_apply_patch"), bool):
        raise ValidationError("agent command policy allow_apply_patch must be a boolean")
    if schema == AGENT_COMMAND_POLICY_SCHEMA_V2:
        validate_native_tool_allowlist(
            policy.get("tool_allowlist"),
            "agent command policy native tool allowlist",
        )


def validate_native_tool_allowlist(value, source: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_NATIVE_TOOL_RULES:
        raise ValidationError(
            "%s must be an array of at most %d native MCP tool names"
            % (source, MAX_NATIVE_TOOL_RULES)
        )
    seen = set()
    for index, name in enumerate(value):
        if (
            not isinstance(name, str)
            or len(name) > MAX_NATIVE_TOOL_NAME_CHARS
            or not SAFE_NATIVE_MCP_TOOL.fullmatch(name)
        ):
            raise ValidationError("%s[%d] is not a safe native MCP tool name" % (source, index))
        if name in seen:
            raise ValidationError("%s contains a duplicate native MCP tool name" % source)
        seen.add(name)


_HOOK_PROGRAM = r'''
import base64,hashlib,json,re,shlex,sys
V1="conductor.agent_command_policy.v1"
V2="conductor.agent_command_policy.v2"
MCP=re.compile(r"^mcp__[A-Za-z0-9][A-Za-z0-9_-]{0,63}__[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
BAD=set(";&|<>()`$*?[]{}~!#")
ASSIGN=re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
def deny(reason):
 print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":reason}},separators=(",",":")))
 raise SystemExit(0)
def parse(command):
 if not isinstance(command,str) or not command or len(command.encode("utf-8"))>65536: raise ValueError()
 quote=None; escaped=False
 for char in command:
  if char in "\r\n\x00" or (ord(char)<32 and char!="\t"): raise ValueError()
  if escaped: escaped=False; continue
  if quote=="'":
   if char=="'": quote=None
   continue
  if quote=='"':
   if char=='"': quote=None
   elif char=="\\": escaped=True
   elif char in {"$","`"}: raise ValueError()
   continue
  if char=="\\": escaped=True
  elif char in {"'",'"'}: quote=char
  elif char in BAD: raise ValueError()
 if quote is not None or escaped: raise ValueError()
 argv=shlex.split(command,comments=False,posix=True)
 if not argv or ASSIGN.match(argv[0]): raise ValueError()
 return argv
try:
 raw=base64.b64decode(sys.argv[1],validate=True)
 if len(raw)>131072 or hashlib.sha256(raw).hexdigest()!=sys.argv[2]: deny("Conductor command policy integrity check failed.")
 policy=json.loads(raw)
 schema=policy.get("schema") if isinstance(policy,dict) else None
 fields={"schema","command_allowlist","allow_apply_patch"}|({"tool_allowlist"} if schema==V2 else set())
 if not isinstance(policy,dict) or schema not in {V1,V2} or set(policy)!=fields or not isinstance(policy.get("allow_apply_patch"),bool): deny("Conductor command policy is invalid.")
 rules=policy.get("command_allowlist")
 if not isinstance(rules,list) or len(rules)>64: deny("Conductor command policy is invalid.")
 for rule in rules:
  if not isinstance(rule,dict) or len(rule)!=1 or next(iter(rule),None) not in {"argv","argv_prefix"}: deny("Conductor command policy is invalid.")
  values=next(iter(rule.values()))
  if not isinstance(values,list) or not values or len(values)>64 or any(not isinstance(value,str) or not value or len(value)>2048 or any(ord(char)<32 and char!="\t" for char in value) for value in values): deny("Conductor command policy is invalid.")
 tools=policy.get("tool_allowlist",[])
 if not isinstance(tools,list) or len(tools)>64 or len(set(tools))!=len(tools) or any(not isinstance(name,str) or len(name)>256 or not MCP.fullmatch(name) for name in tools): deny("Conductor command policy is invalid.")
 data=sys.stdin.buffer.read(1048577)
 if len(data)>1048576: deny("Conductor command policy hook input is too large.")
 event=json.loads(data)
 if not isinstance(event,dict) or event.get("hook_event_name")!="PreToolUse": deny("Conductor command policy received invalid hook input.")
 tool=event.get("tool_name")
 if tool=="apply_patch":
  if policy["allow_apply_patch"]: print("{}")
  else: deny("Conductor profile does not allow apply_patch.")
  raise SystemExit(0)
 if tool in tools: print("{}"); raise SystemExit(0)
 if tool!="Bash": deny("Conductor profile does not allow this tool.")
 item=event.get("tool_input")
 command=item.get("command") if isinstance(item,dict) else None
 try: argv=parse(command)
 except Exception: deny("Conductor profile allows only one expansion-free simple command.")
 for rule in rules:
  if "argv" in rule and argv==rule["argv"]: print("{}"); raise SystemExit(0)
  if "argv_prefix" in rule and argv[:len(rule["argv_prefix"])]==rule["argv_prefix"]: print("{}"); raise SystemExit(0)
 deny("Bash command is outside the Conductor profile allowlist.")
except SystemExit: raise
except Exception: deny("Conductor command policy hook failed closed.")
'''.strip()
