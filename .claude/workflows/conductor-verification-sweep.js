export const meta = {
  "description": "Run Conductor's local verification sweep through the safe saved-workflow adapter.",
  "name": "conductor-verification-sweep"
};

export const workflow = {
  "default_timeout_seconds": 240,
  "description": "Run conservative local validation commands and collect evidence from a saved workflow command.",
  "max_workers": 1,
  "mode": "read_only",
  "name": "verification-sweep",
  "risk": "low",
  "schema": "conductor.workflow.v1",
  "steps": [
    {
      "capture": "verification/runtime-help.txt",
      "command": [
        "python3",
        "-B",
        "-m",
        "conductor_runtime",
        "--help"
      ],
      "description": "Verify the runtime CLI imports and exposes help.",
      "id": "runtime-help",
      "kind": "shell",
      "risk": "low",
      "writes": false
    },
    {
      "capture": "verification/unittest.txt",
      "command": [
        "python3",
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests"
      ],
      "description": "Run focused runtime and project tests.",
      "id": "runtime-tests",
      "kind": "shell",
      "risk": "low",
      "writes": true
    },
    {
      "capture": "verification/skill-audit.txt",
      "command": [
        "python3",
        "-B",
        "tools/run_local_audit.py",
        "codex-conductor"
      ],
      "description": "Ensure the prompt-only skill still passes its local audit.",
      "id": "skill-audit",
      "kind": "shell",
      "risk": "low",
      "writes": false
    }
  ]
};
