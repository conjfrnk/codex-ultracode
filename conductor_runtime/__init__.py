"""Companion runtime for Codex Conductor workflows."""

import sys

__version__ = "0.174.0"
MINIMUM_PYTHON = (3, 12)


def require_supported_python(version_info=None) -> None:
    """Reject interpreters outside the runtime's tested support window."""
    detected = sys.version_info if version_info is None else version_info
    detected_version = tuple(detected[:3])
    if detected_version[:2] < MINIMUM_PYTHON:
        raise RuntimeError(
            "Codex Conductor requires Python %d.%d or newer; found Python %d.%d.%d"
            % (*MINIMUM_PYTHON, *detected_version)
        )
