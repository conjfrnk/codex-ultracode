#!/usr/bin/env python3
"""Validate repository custom-agent profiles and optionally probe Codex discovery."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "codex-agents"
MAX_PROFILE_FILES = 32
MAX_PROFILE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
PROFILE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ROLE_HEADER = re.compile(r"^([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?): \{$")
REQUIRED_STRING_FIELDS = ("name", "description", "developer_instructions", "sandbox_mode")
ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}
SAFE_ENVIRONMENT_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
}


class AgentCheckError(ValueError):
    """Raised when source profiles or Codex discovery evidence are invalid."""


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    developer_instructions: str
    sandbox_mode: str
    path: Path
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class CodexDiscoveryResult:
    codex_version: str
    request_path: str
    request_count: int
    returncode: int
    advertised_agent_types: tuple[str, ...]


class _CaptureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CaptureHandler)
        self.capture_lock = threading.Lock()
        self.request_bodies: list[bytes] = []
        self.request_paths: list[str] = []


class _CaptureHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, b'{"error":{"message":"invalid content length"}}')
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._reply(413, b'{"error":{"message":"request size rejected"}}')
            return
        body = self.rfile.read(length)
        with self.server.capture_lock:
            self.server.request_paths.append(self.path)
            self.server.request_bodies.append(body)
        self._reply(400, b'{"error":{"message":"custom-agent discovery request captured"}}')

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _read_profile(path: Path) -> tuple[Mapping[str, Any], bytes]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgentCheckError("cannot inspect agent profile %s: %s" % (path, exc)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AgentCheckError("agent profile must be a regular non-symlink file: %s" % path)
    if metadata.st_size > MAX_PROFILE_BYTES:
        raise AgentCheckError("agent profile exceeds %d bytes: %s" % (MAX_PROFILE_BYTES, path))
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentCheckError("agent profile must be readable UTF-8: %s" % path) from exc
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AgentCheckError("agent profile is invalid TOML: %s: %s" % (path, exc)) from exc
    if not isinstance(parsed, dict):
        raise AgentCheckError("agent profile must contain a TOML table: %s" % path)
    return parsed, content


def validate_agent_profiles(profile_dir: Path | str) -> tuple[AgentProfile, ...]:
    """Return validated source profiles in canonical name order."""

    root = Path(profile_dir)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise AgentCheckError("cannot inspect agent profile directory %s: %s" % (root, exc)) from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise AgentCheckError("agent profile directory must be a real directory: %s" % root)

    try:
        entries = sorted(root.iterdir(), key=lambda candidate: candidate.name)
    except OSError as exc:
        raise AgentCheckError("cannot list agent profile directory %s: %s" % (root, exc)) from exc
    unexpected = [path.name for path in entries if path.suffix != ".toml"]
    if unexpected:
        raise AgentCheckError("agent profile directory contains unexpected entries: %s" % ", ".join(unexpected))
    if not entries:
        raise AgentCheckError("agent profile directory contains no TOML profiles: %s" % root)
    if len(entries) > MAX_PROFILE_FILES:
        raise AgentCheckError("agent profile directory exceeds %d files" % MAX_PROFILE_FILES)

    profiles: list[AgentProfile] = []
    seen_names: set[str] = set()
    for path in entries:
        parsed, content = _read_profile(path)
        values: dict[str, str] = {}
        for field_name in REQUIRED_STRING_FIELDS:
            value = parsed.get(field_name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise AgentCheckError("agent profile %s must define a non-empty string %s" % (path, field_name))
            values[field_name] = value

        name = values["name"]
        if PROFILE_NAME.fullmatch(name) is None:
            raise AgentCheckError("agent profile %s has an invalid name: %s" % (path, name))
        if name in seen_names:
            raise AgentCheckError("agent profile name is duplicated: %s" % name)
        seen_names.add(name)

        description = values["description"]
        if description != description.strip():
            raise AgentCheckError("agent profile %s description has surrounding whitespace" % path)
        sandbox_mode = values["sandbox_mode"]
        if sandbox_mode not in ALLOWED_SANDBOX_MODES:
            raise AgentCheckError(
                "agent profile %s sandbox_mode must be one of: %s" % (path, ", ".join(sorted(ALLOWED_SANDBOX_MODES)))
            )

        nicknames = parsed.get("nickname_candidates")
        if nicknames is not None:
            if (
                not isinstance(nicknames, list)
                or not nicknames
                or any(not isinstance(value, str) or not value.strip() for value in nicknames)
                or len(set(nicknames)) != len(nicknames)
            ):
                raise AgentCheckError("agent profile %s nickname_candidates must be unique non-empty strings" % path)

        profiles.append(
            AgentProfile(
                name=name,
                description=description,
                developer_instructions=values["developer_instructions"],
                sandbox_mode=sandbox_mode,
                path=path,
                content=content,
            )
        )
    for profile in profiles:
        if profile.path.stem != profile.name:
            raise AgentCheckError(
                "agent profile filename must match its name: %s != %s" % (profile.path.stem, profile.name)
            )
    return tuple(sorted(profiles, key=lambda profile: profile.name))


def validate_installed_profiles(
    profiles: Sequence[AgentProfile],
    codex_home: Path | str,
) -> Path:
    """Verify that every source profile is installed byte-for-byte, ignoring unrelated profiles."""

    installed_root = Path(codex_home).expanduser() / "agents"
    try:
        metadata = installed_root.lstat()
    except OSError as exc:
        raise AgentCheckError("cannot inspect installed agent directory %s: %s" % (installed_root, exc)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentCheckError("installed agent directory must be a real directory: %s" % installed_root)
    for profile in profiles:
        installed_path = installed_root / profile.path.name
        _parsed, installed_content = _read_profile(installed_path)
        if installed_content != profile.content:
            raise AgentCheckError("installed agent profile differs from source: %s" % profile.name)
    return installed_root


def extract_advertised_agent_roles(request: Mapping[str, Any]) -> dict[str, str]:
    """Extract role names and descriptions from a Codex spawn_agent tool schema."""

    tools = request.get("tools")
    if not isinstance(tools, list):
        raise AgentCheckError("Codex request does not contain a tools array")
    spawn_tools: list[Mapping[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        candidates = tool.get("tools") if tool.get("type") == "namespace" else [tool]
        if not isinstance(candidates, list):
            continue
        spawn_tools.extend(
            candidate
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("name") == "spawn_agent"
        )
    if len(spawn_tools) != 1:
        raise AgentCheckError("Codex request must advertise exactly one spawn_agent tool")

    try:
        role_description = spawn_tools[0]["parameters"]["properties"]["agent_type"]["description"]
    except (KeyError, TypeError) as exc:
        raise AgentCheckError("Codex spawn_agent schema omits agent_type role metadata") from exc
    if not isinstance(role_description, str) or "Available roles:" not in role_description:
        raise AgentCheckError("Codex spawn_agent schema omits the available role list")

    lines = role_description.split("Available roles:", 1)[1].strip().splitlines()
    roles: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = ROLE_HEADER.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        name = match.group(1)
        index += 1
        description_lines = []
        while index < len(lines) and lines[index] != "}":
            description_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise AgentCheckError("Codex available role block is unterminated: %s" % name)
        if name in roles:
            raise AgentCheckError("Codex available role list duplicates: %s" % name)
        roles[name] = "\n".join(description_lines).strip()
        index += 1
    if not roles:
        raise AgentCheckError("Codex available role list is empty")
    return roles


def validate_codex_discovery_request(
    request: Mapping[str, Any],
    profiles: Sequence[AgentProfile],
) -> dict[str, str]:
    """Verify that a real Codex request advertises every validated source profile."""

    if request.get("model") != "probe-model" or request.get("stream") is not True:
        raise AgentCheckError("Codex discovery request did not use the isolated streaming probe provider")
    advertised = extract_advertised_agent_roles(request)
    for profile in profiles:
        if profile.name not in advertised:
            raise AgentCheckError("Codex did not advertise custom agent: %s" % profile.name)
        if profile.description != advertised[profile.name]:
            raise AgentCheckError("Codex advertised the wrong description for custom agent: %s" % profile.name)
    return advertised


def _resolve_codex_executable(value: str | Path) -> Path:
    requested = os.fspath(value)
    discovered = shutil.which(requested)
    candidate = Path(discovered if discovered is not None else requested).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise AgentCheckError("Codex executable is unavailable: %s" % requested) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise AgentCheckError("Codex executable is not an executable regular file: %s" % resolved)
    return resolved


def _probe_environment(root: Path, codex_home: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENVIRONMENT_KEYS}
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "NO_PROXY": "127.0.0.1,localhost",
            "PROBE_API_KEY": "not-a-secret",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return environment


def probe_codex_agent_discovery(
    codex: str | Path,
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
    *,
    timeout_seconds: float = 15.0,
) -> CodexDiscoveryResult:
    """Capture the real Codex spawn schema using an isolated loopback provider."""

    profiles = validate_agent_profiles(profile_dir)
    executable = _resolve_codex_executable(codex)
    server = _CaptureServer()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="codex-agent-discovery-") as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            agents_dir = codex_home / "agents"
            workspace = root / "workspace"
            agents_dir.mkdir(parents=True)
            workspace.mkdir()
            for profile in profiles:
                (agents_dir / profile.path.name).write_bytes(profile.content)

            port = server.server_address[1]
            (codex_home / "config.toml").write_text(
                f"""model = "probe-model"
model_provider = "probe"

[model_providers.probe]
name = "Local custom-agent discovery probe"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
env_key = "PROBE_API_KEY"
request_max_retries = 0
stream_max_retries = 0
""",
                encoding="utf-8",
            )
            environment = _probe_environment(root, codex_home)
            try:
                version = subprocess.run(
                    [str(executable), "--version"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                    check=False,
                )
                completed = subprocess.run(
                    [
                        str(executable),
                        "--strict-config",
                        "-s",
                        "read-only",
                        "-a",
                        "never",
                        "exec",
                        "--ephemeral",
                        "--skip-git-repo-check",
                        "probe custom-agent discovery",
                    ],
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentCheckError("Codex discovery probe timed out after %.1f seconds" % timeout_seconds) from exc
            except OSError as exc:
                raise AgentCheckError("failed to execute Codex discovery probe: %s" % exc) from exc

            if version.returncode != 0 or not version.stdout.strip():
                raise AgentCheckError("Codex --version failed in the isolated probe environment")
            with server.capture_lock:
                request_paths = tuple(server.request_paths)
                request_bodies = tuple(server.request_bodies)
            if len(request_bodies) != 1:
                detail = completed.stderr.strip().replace("\n", " ")[:500]
                raise AgentCheckError(
                    "Codex discovery probe expected one loopback request, observed %d%s"
                    % (len(request_bodies), (": " + detail) if detail else "")
                )
            if request_paths != ("/v1/responses",):
                raise AgentCheckError("Codex discovery probe used an unexpected request path: %s" % request_paths[0])
            if completed.returncode == 0:
                raise AgentCheckError("Codex discovery probe unexpectedly accepted the synthetic provider failure")
            try:
                request = json.loads(request_bodies[0].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AgentCheckError("Codex discovery request was not valid UTF-8 JSON") from exc
            if not isinstance(request, dict):
                raise AgentCheckError("Codex discovery request must be a JSON object")
            advertised = validate_codex_discovery_request(request, profiles)
            return CodexDiscoveryResult(
                codex_version=version.stdout.strip(),
                request_path=request_paths[0],
                request_count=len(request_bodies),
                returncode=completed.returncode,
                advertised_agent_types=tuple(sorted(advertised)),
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Codex custom-agent profiles and optionally prove real CLI discovery over loopback."
    )
    parser.add_argument(
        "profile_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help="directory containing standalone custom-agent TOML files",
    )
    parser.add_argument(
        "--codex",
        metavar="PATH",
        help="run the offline loopback discovery probe with this Codex executable",
    )
    parser.add_argument(
        "--installed",
        action="store_true",
        help="compare every source profile with the active Codex installation",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex home to inspect with --installed (defaults to CODEX_HOME or ~/.codex)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable result")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profiles = validate_agent_profiles(args.profile_dir)
        configured_codex_home = args.codex_home
        if configured_codex_home is None:
            configured = os.environ.get("CODEX_HOME")
            configured_codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
        installed_root = validate_installed_profiles(profiles, configured_codex_home) if args.installed else None
        probe = probe_codex_agent_discovery(args.codex, args.profile_dir) if args.codex else None
    except AgentCheckError as exc:
        print("FAILED: %s" % exc, file=sys.stderr)
        return 1

    payload: dict[str, Any] = {
        "profile_dir": str(args.profile_dir.resolve()),
        "profiles": [
            {
                "name": profile.name,
                "description": profile.description,
                "sandbox_mode": profile.sandbox_mode,
            }
            for profile in profiles
        ],
    }
    if probe is not None:
        payload["codex_probe"] = asdict(probe)
    if installed_root is not None:
        payload["installed_profile_dir"] = str(installed_root.resolve())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("OK: validated %d custom-agent profile(s): %s" % (len(profiles), ", ".join(p.name for p in profiles)))
        if installed_root is not None:
            print("OK: installed profiles match source under %s" % installed_root)
        if probe is not None:
            print("OK: %s advertised every source profile via %s" % (probe.codex_version, probe.request_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
