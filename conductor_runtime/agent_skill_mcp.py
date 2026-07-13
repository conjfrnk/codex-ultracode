import hashlib
import json
import re
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .agent_tool_policy import MAX_COMMAND_ARGV, validate_command_allowlist
from .errors import ValidationError


MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES = 8
MAX_AGENT_PROFILE_SKILL_MCP_TOOLS = 32
MAX_AGENT_PROFILE_SKILL_MCP_TOTAL_TOOLS = 64
MAX_AGENT_PROFILE_SKILL_MCP_NAME_CHARS = 64
MAX_AGENT_PROFILE_SKILL_MCP_TOOL_NAME_CHARS = 128
MAX_AGENT_PROFILE_SKILL_MCP_URL_CHARS = 2048
MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS = 16
MAX_AGENT_PROFILE_SKILL_MCP_ENV_VAR_CHARS = 128
MAX_AGENT_PROFILE_SKILL_MCP_HEADER_NAME_CHARS = 128
MAX_AGENT_PROFILE_SKILL_MCP_SECRET_BYTES = 16 * 1024
MIN_AGENT_PROFILE_SKILL_MCP_SECRET_BYTES = 8
MAX_AGENT_PROFILE_SKILL_MCP_SCRIPT_PATH_CHARS = 512

SAFE_MCP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SAFE_MCP_ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SAFE_MCP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
FORBIDDEN_MCP_ENV_HEADER_NAMES = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def validate_skill_mcp_dependencies(
    profile: Dict,
    source: str = "agent profile",
) -> List[Dict]:
    value = profile.get("skill_mcp_dependencies", [])
    if not isinstance(value, list) or len(value) > MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES:
        raise ValidationError(
            "%s skill_mcp_dependencies must be an array of at most %d dependencies"
            % (source, MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES)
        )
    selected_skills = profile.get("skills", [])
    dependencies = []
    seen_names = set()
    seen_tools = set()
    for index, item in enumerate(value):
        label = "%s skill_mcp_dependencies[%d]" % (source, index)
        if not isinstance(item, dict):
            raise ValidationError("%s must be an object" % label)
        transport = item.get("transport")
        if transport is None:
            required = {"skill", "name", "url", "tools"}
            allowed = required | {"auth"}
        elif transport == "stdio":
            required = {"skill", "name", "transport", "script", "args", "tools"}
            allowed = required
        else:
            raise ValidationError(
                "%s transport must be stdio when specified" % label
            )
        if not required <= set(item) or not set(item) <= allowed:
            if transport == "stdio":
                raise ValidationError(
                    "%s stdio route must contain only skill, name, transport, script, args, and tools"
                    % label
                )
            raise ValidationError(
                "%s must contain skill, name, url, and tools, with only optional auth" % label
            )
        skill = item["skill"]
        if not isinstance(skill, str) or skill not in selected_skills:
            raise ValidationError("%s skill must name an already selected Skill" % label)
        name = item["name"]
        if (
            not isinstance(name, str)
            or len(name) > MAX_AGENT_PROFILE_SKILL_MCP_NAME_CHARS
            or not SAFE_MCP_NAME.fullmatch(name)
        ):
            raise ValidationError("%s name must be a safe MCP server identifier" % label)
        if name in seen_names:
            raise ValidationError("%s name must be unique within the profile" % label)
        seen_names.add(name)
        if transport == "stdio":
            script = _validate_skill_mcp_script(item["script"], "%s script" % label)
            args = item["args"]
            if not isinstance(args, list) or len(args) > MAX_COMMAND_ARGV - 1:
                raise ValidationError(
                    "%s args must be an array of at most %d arguments"
                    % (label, MAX_COMMAND_ARGV - 1)
                )
            validate_command_allowlist(
                [{"argv": [script] + list(args)}],
                "%s command" % label,
            )
        else:
            url = item["url"]
            _validate_https_mcp_url(url, "%s url" % label)
        tools = item["tools"]
        if (
            not isinstance(tools, list)
            or not tools
            or len(tools) > MAX_AGENT_PROFILE_SKILL_MCP_TOOLS
        ):
            raise ValidationError(
                "%s tools must be a non-empty array of at most %d tool names"
                % (label, MAX_AGENT_PROFILE_SKILL_MCP_TOOLS)
            )
        local_tools = set()
        cleaned_tools = []
        for tool_index, tool in enumerate(tools):
            if (
                not isinstance(tool, str)
                or len(tool) > MAX_AGENT_PROFILE_SKILL_MCP_TOOL_NAME_CHARS
                or not SAFE_MCP_TOOL_NAME.fullmatch(tool)
            ):
                raise ValidationError(
                    "%s tools[%d] must be a safe MCP tool name" % (label, tool_index)
                )
            if tool in local_tools:
                raise ValidationError("%s tools contains a duplicate name" % label)
            local_tools.add(tool)
            native_name = native_mcp_tool_name(name, tool)
            if native_name in seen_tools:
                raise ValidationError("%s produces a duplicate native MCP tool name" % label)
            seen_tools.add(native_name)
            cleaned_tools.append(tool)
        if transport == "stdio":
            dependency = {
                "skill": skill,
                "name": name,
                "transport": "stdio",
                "script": script,
                "args": list(args),
                "tools": cleaned_tools,
            }
        else:
            dependency = {
                "skill": skill,
                "name": name,
                "url": url,
                "tools": cleaned_tools,
            }
            if "auth" in item:
                dependency["auth"] = _validate_skill_mcp_auth(
                    item["auth"], "%s auth" % label
                )
        dependencies.append(dependency)
    if len(seen_tools) > MAX_AGENT_PROFILE_SKILL_MCP_TOTAL_TOOLS:
        raise ValidationError(
            "%s skill_mcp_dependencies expose at most %d total tools"
            % (source, MAX_AGENT_PROFILE_SKILL_MCP_TOTAL_TOOLS)
        )
    return dependencies


def skill_mcp_native_tool_allowlist(profile: Dict) -> List[str]:
    return [
        native_mcp_tool_name(dependency["name"], tool)
        for dependency in validate_skill_mcp_dependencies(profile)
        for tool in dependency["tools"]
    ]


def native_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return "mcp__%s__%s" % (server_name, tool_name)


def skill_mcp_dependencies_sha256(dependencies: List[Dict]) -> str:
    payload = json.dumps(
        dependencies,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def skill_mcp_approval_token(name: str) -> str:
    if not isinstance(name, str) or not SAFE_MCP_NAME.fullmatch(name):
        raise ValidationError("Skill MCP approval name is invalid")
    return "skill-mcp:%s" % name


def skill_mcp_auth_approval_token(name: str) -> str:
    if not isinstance(name, str) or not SAFE_MCP_NAME.fullmatch(name):
        raise ValidationError("Skill MCP auth approval name is invalid")
    return "skill-mcp-auth:%s" % name


def skill_mcp_stdio_approval_token(name: str) -> str:
    if not isinstance(name, str) or not SAFE_MCP_NAME.fullmatch(name):
        raise ValidationError("Skill MCP stdio approval name is invalid")
    return "skill-mcp-stdio:%s" % name


def skill_mcp_http_dependencies(dependencies: List[Dict]) -> List[Dict]:
    return [
        dependency
        for dependency in _validated_dependency_list(dependencies, "Skill MCP HTTP routes")
        if dependency.get("transport") != "stdio"
    ]


def skill_mcp_stdio_dependencies(dependencies: List[Dict]) -> List[Dict]:
    return [
        dependency
        for dependency in _validated_dependency_list(dependencies, "Skill MCP stdio routes")
        if dependency.get("transport") == "stdio"
    ]


def skill_mcp_auth_env_vars(dependencies: List[Dict]) -> List[str]:
    profile = {
        "skills": [dependency.get("skill") for dependency in dependencies],
        "skill_mcp_dependencies": dependencies,
    }
    cleaned = validate_skill_mcp_dependencies(profile, "Skill MCP auth environment")
    names = set()
    for dependency in cleaned:
        auth = dependency.get("auth", {})
        bearer = auth.get("bearer_token_env_var")
        if bearer is not None:
            names.add(bearer)
        names.update(auth.get("env_http_headers", {}).values())
    return sorted(names)


def resolve_skill_mcp_auth_secrets(
    dependencies: List[Dict],
    environ: Mapping[str, str],
) -> Tuple[str, ...]:
    resolved = resolve_skill_mcp_auth(dependencies, environ)
    secrets = {
        value
        for route in resolved
        for value in route["secret_values"]
        if value
    }
    return tuple(sorted(secrets, key=lambda value: (-len(value), value)))


def resolve_skill_mcp_auth(
    dependencies: List[Dict],
    environ: Mapping[str, str],
) -> List[Dict]:
    profile = {
        "skills": [dependency.get("skill") for dependency in dependencies],
        "skill_mcp_dependencies": dependencies,
    }
    cleaned = validate_skill_mcp_dependencies(profile, "Skill MCP auth environment")
    resolved = []
    for dependency in cleaned:
        auth = dependency.get("auth", {})
        if not auth:
            continue
        headers = {}
        secret_values = []
        bearer = auth.get("bearer_token_env_var")
        if bearer is not None:
            value = _require_skill_mcp_secret(environ, bearer, bearer=True)
            headers["Authorization"] = "Bearer %s" % value
            secret_values.append(value)
        for header, env_var in auth.get("env_http_headers", {}).items():
            value = _require_skill_mcp_secret(
                environ,
                env_var,
                bearer=False,
            )
            headers[header] = value
            secret_values.append(value)
        resolved.append(
            {
                "name": dependency["name"],
                "url": dependency["url"],
                "headers": headers,
                "secret_values": tuple(
                    sorted(set(secret_values), key=lambda value: (-len(value), value))
                ),
            }
        )
    return resolved


def codex_skill_mcp_config_arg(
    dependencies: List[Dict],
    *,
    endpoint_overrides: Optional[Mapping[str, str]] = None,
    command_overrides: Optional[Mapping[str, List[str]]] = None,
) -> str:
    profile = {
        "skills": [dependency.get("skill") for dependency in dependencies],
        "skill_mcp_dependencies": dependencies,
    }
    cleaned = validate_skill_mcp_dependencies(profile, "Codex Skill MCP config")
    overrides = dict(endpoint_overrides or {})
    commands = dict(command_overrides or {})
    http_names = {
        dependency["name"]
        for dependency in cleaned
        if dependency.get("transport") != "stdio"
    }
    stdio_names = {
        dependency["name"]
        for dependency in cleaned
        if dependency.get("transport") == "stdio"
    }
    if set(overrides) - http_names:
        raise ValidationError("Codex Skill MCP endpoint override names are invalid")
    if set(commands) - stdio_names:
        raise ValidationError("Codex Skill MCP command override names are invalid")
    for name, url in overrides.items():
        _validate_loopback_mcp_url(url, "Codex Skill MCP endpoint override %s" % name)
    for name, argv in commands.items():
        validate_command_allowlist(
            [{"argv": argv}],
            "Codex Skill MCP command override %s" % name,
        )
    servers = []
    for dependency in cleaned:
        tools = json.dumps(dependency["tools"], ensure_ascii=True, separators=(",", ":"))
        if dependency.get("transport") == "stdio":
            argv = commands.get(
                dependency["name"],
                ["conductor-skill-mcp-stdio-proxy-unprepared"],
            )
            servers.append(
                "%s={command=%s,args=%s,enabled=true,required=true,enabled_tools=%s,"
                "default_tools_approval_mode=\"approve\",startup_timeout_sec=10,tool_timeout_sec=60}"
                % (
                    dependency["name"],
                    json.dumps(argv[0], ensure_ascii=True),
                    json.dumps(argv[1:], ensure_ascii=True, separators=(",", ":")),
                    tools,
                )
            )
            continue
        auth_fields = []
        auth = dependency.get("auth", {})
        if dependency["name"] not in overrides and "bearer_token_env_var" in auth:
            auth_fields.append(
                "bearer_token_env_var=%s"
                % json.dumps(auth["bearer_token_env_var"], ensure_ascii=True)
            )
        if dependency["name"] not in overrides and "env_http_headers" in auth:
            headers = ",".join(
                "%s=%s"
                % (
                    json.dumps(header, ensure_ascii=True),
                    json.dumps(auth["env_http_headers"][header], ensure_ascii=True),
                )
                for header in sorted(auth["env_http_headers"], key=str.lower)
            )
            auth_fields.append("env_http_headers={%s}" % headers)
        auth_fragment = ("," + ",".join(auth_fields)) if auth_fields else ""
        servers.append(
            "%s={url=%s,enabled=true,required=true,enabled_tools=%s,"
            "default_tools_approval_mode=\"approve\",startup_timeout_sec=10,tool_timeout_sec=60%s}"
            % (
                dependency["name"],
                json.dumps(
                    overrides.get(dependency["name"], dependency["url"]),
                    ensure_ascii=True,
                ),
                tools,
                auth_fragment,
            )
        )
    return "mcp_servers={%s}" % ",".join(servers)


def _validated_dependency_list(dependencies: List[Dict], source: str) -> List[Dict]:
    profile = {
        "skills": [dependency.get("skill") for dependency in dependencies],
        "skill_mcp_dependencies": dependencies,
    }
    return validate_skill_mcp_dependencies(profile, source)


def _validate_skill_mcp_script(value, source: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > MAX_AGENT_PROFILE_SKILL_MCP_SCRIPT_PATH_CHARS
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or not value.startswith("scripts/")
    ):
        raise ValidationError(
            "%s must be a bounded canonical path under scripts/" % source
        )
    return value


def _validate_https_mcp_url(value, source: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > MAX_AGENT_PROFILE_SKILL_MCP_URL_CHARS
        or any(ord(char) < 32 for char in value)
    ):
        raise ValidationError("%s must be a bounded HTTPS URL" % source)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("%s must be a valid HTTPS URL" % source) from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or any(char.isspace() for char in value)
    ):
        raise ValidationError(
            "%s must be an HTTPS URL without embedded credentials, query, or fragment" % source
        )


def _validate_loopback_mcp_url(value, source: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_AGENT_PROFILE_SKILL_MCP_URL_CHARS:
        raise ValidationError("%s must be a bounded loopback URL" % source)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("%s must be a valid loopback URL" % source) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("%s must be a pinned HTTP 127.0.0.1 URL" % source)


def _validate_skill_mcp_auth(value, source: str) -> Dict:
    if (
        not isinstance(value, dict)
        or not value
        or not set(value) <= {"bearer_token_env_var", "env_http_headers"}
    ):
        raise ValidationError(
            "%s must contain only bearer_token_env_var and/or env_http_headers" % source
        )
    cleaned = {}
    if "bearer_token_env_var" in value:
        cleaned["bearer_token_env_var"] = _validate_skill_mcp_env_var(
            value["bearer_token_env_var"],
            "%s bearer_token_env_var" % source,
        )
    if "env_http_headers" in value:
        headers = value["env_http_headers"]
        if (
            not isinstance(headers, dict)
            or not headers
            or len(headers) > MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS
        ):
            raise ValidationError(
                "%s env_http_headers must be a non-empty object of at most %d headers"
                % (source, MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS)
            )
        cleaned_headers = {}
        seen_headers = set()
        for header, env_var in headers.items():
            if (
                not isinstance(header, str)
                or len(header) > MAX_AGENT_PROFILE_SKILL_MCP_HEADER_NAME_CHARS
                or not SAFE_MCP_HEADER_NAME.fullmatch(header)
            ):
                raise ValidationError("%s contains an unsafe HTTP header name" % source)
            normalized = header.lower()
            if normalized in seen_headers:
                raise ValidationError("%s contains a case-insensitive duplicate header" % source)
            if normalized in FORBIDDEN_MCP_ENV_HEADER_NAMES:
                raise ValidationError("%s cannot configure reserved HTTP header %s" % (source, header))
            seen_headers.add(normalized)
            cleaned_headers[header] = _validate_skill_mcp_env_var(
                env_var,
                "%s env_http_headers[%s]" % (source, header),
            )
        cleaned["env_http_headers"] = cleaned_headers
    return cleaned


def _validate_skill_mcp_env_var(value, source: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_AGENT_PROFILE_SKILL_MCP_ENV_VAR_CHARS
        or not SAFE_MCP_ENV_VAR.fullmatch(value)
    ):
        raise ValidationError("%s must be a safe environment variable name" % source)
    return value


def _require_skill_mcp_secret(
    environ: Mapping[str, str],
    env_var: str,
    *,
    bearer: bool,
) -> str:
    value = environ.get(env_var)
    if not isinstance(value, str):
        raise ValidationError(
            "authenticated selected Skill MCP dependency requires environment variable %s"
            % env_var
        )
    encoded = value.encode("utf-8")
    if (
        len(encoded) < MIN_AGENT_PROFILE_SKILL_MCP_SECRET_BYTES
        or len(encoded) > MAX_AGENT_PROFILE_SKILL_MCP_SECRET_BYTES
        or value.strip() != value
        or "\r" in value
        or "\n" in value
        or "\x00" in value
        or (bearer and any(char.isspace() for char in value))
    ):
        raise ValidationError(
            "environment variable %s is not a bounded safe %s credential"
            % (env_var, "bearer" if bearer else "HTTP header")
        )
    return value
