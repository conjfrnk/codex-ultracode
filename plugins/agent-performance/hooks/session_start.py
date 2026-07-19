#!/usr/bin/env python3
"""Announce the Codex Ultracode capabilities available to a new thread."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tomllib


READY_MESSAGE = (
    "Codex with Ultracode — Conductor orchestration + specialist agent workflows ready."
)
NO_CONDUCTOR_MESSAGE = (
    "Codex with Ultracode — specialist agent workflows ready (Conductor not detected)."
)


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _load_config(codex_home: Path) -> dict | None:
    try:
        source = (codex_home / "config.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        return None
    try:
        return tomllib.loads(source)
    except tomllib.TOMLDecodeError:
        return None


def _configured_path(raw_path: object, *, relative_to: Path) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else relative_to / path


def _skill_enabled(skill_path: Path, *, codex_home: Path, config: dict) -> bool:
    if not skill_path.is_file():
        return False

    skills = config.get("skills")
    overrides = skills.get("config") if isinstance(skills, dict) else None
    if not isinstance(overrides, list):
        return True
    expected = skill_path.resolve()
    enabled = True
    for settings in overrides:
        if not isinstance(settings, dict):
            continue
        candidate = _configured_path(settings.get("path"), relative_to=codex_home)
        if candidate is None or candidate.resolve() != expected:
            continue
        value = settings.get("enabled")
        if not isinstance(value, bool):
            return False
        enabled = value
    return enabled


def _standalone_skill_enabled(codex_home: Path, config: dict) -> bool:
    return _skill_enabled(
        codex_home / "skills" / "codex-conductor" / "SKILL.md",
        codex_home=codex_home,
        config=config,
    )


def _enabled_plugin_installed(codex_home: Path, config: dict) -> bool:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return False
    for name, settings in plugins.items():
        if not isinstance(name, str) or not isinstance(settings, dict):
            continue
        plugin_name, separator, marketplace = name.partition("@")
        if plugin_name != "codex-conductor" or not separator or settings.get("enabled") is not True:
            continue
        if marketplace in {"", ".", ".."} or Path(marketplace).name != marketplace:
            continue
        versions = codex_home / "plugins" / "cache" / marketplace / plugin_name
        try:
            if any(
                _skill_enabled(
                    version / "skills" / "codex-conductor" / "SKILL.md",
                    codex_home=codex_home,
                    config=config,
                )
                for version in versions.iterdir()
                if version.is_dir()
            ):
                return True
        except OSError:
            continue
    return False


def _conductor_available(codex_home: Path) -> bool:
    config = _load_config(codex_home)
    if config is None:
        return False
    return _standalone_skill_enabled(codex_home, config) or _enabled_plugin_installed(
        codex_home, config
    )


def _identity_message(codex_home: Path) -> str:
    return READY_MESSAGE if _conductor_available(codex_home) else NO_CONDUCTOR_MESSAGE


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--identity-message"]:
        print(_identity_message(_codex_home()))
        return 0
    if arguments:
        return 2

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    if event.get("hook_event_name") != "SessionStart" or event.get("source") != "startup":
        return 0
    if os.environ.get("CODEX_ULTRACODE_STARTUP_SHOWN") == "1":
        return 0

    message = _identity_message(_codex_home())
    print(json.dumps({"continue": True, "systemMessage": message}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
