#!/usr/bin/env python3
"""Deterministic local Codex CLI stand-in for packaged runtime smoke tests."""

import json
import os
from pathlib import Path
import sys
import time


SESSION_ID = "019f0000-0000-7000-8000-000000000001"


def main() -> int:
    prompt = sys.stdin.read()
    delay_text = os.environ.get("CONDUCTOR_FAKE_CODEX_DELAY_SECONDS", "0")
    try:
        delay_seconds = float(delay_text)
    except ValueError:
        print("invalid CONDUCTOR_FAKE_CODEX_DELAY_SECONDS", file=sys.stderr)
        return 2
    if not 0 <= delay_seconds <= 60:
        print("CONDUCTOR_FAKE_CODEX_DELAY_SECONDS must be from 0 to 60", file=sys.stderr)
        return 2
    if delay_seconds:
        time.sleep(delay_seconds)
    try:
        output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    except (ValueError, IndexError):
        print("missing --output-last-message", file=sys.stderr)
        return 2
    resumed = len(sys.argv) > 2 and sys.argv[1:3] == ["exec", "resume"]
    if resumed and SESSION_ID not in sys.argv:
        print("resume command did not include the expected session id", file=sys.stderr)
        return 2
    if "strict read-only completion verifier" in prompt:
        verdict = {
            "satisfied": True,
            "feedback": "",
            "evidence": ["deterministic fake verifier evidence"],
            "confidence": "high",
        }
        output.write_text(json.dumps(verdict), encoding="utf-8")
        print(
            json.dumps(
                {
                    "type": "turn.completed",
                    "model": "deterministic-smoke-model",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            )
        )
        return 0
    workflow = {
        "schema": "conductor.workflow.v1",
        "name": "packaged-model-background-smoke",
        "description": "Packaged model-to-background smoke workflow.",
        "mode": "review",
        "risk": "low",
        "max_workers": 1,
        "steps": [
            {
                "id": "record-result",
                "kind": "write_artifact",
                "risk": "low",
                "output": "packaged-model-background.txt",
                "content": "packaged model background workflow completed\n",
            }
        ],
    }
    if os.environ.get("CONDUCTOR_FAKE_CODEX_ECHO_PROMPT") == "1":
        output.write_text(prompt, encoding="utf-8")
    else:
        output.write_text(json.dumps(workflow), encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": SESSION_ID}))
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "model": "deterministic-smoke-model",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
