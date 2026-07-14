#!/usr/bin/env python3
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("codex-conductor")
tool_dir = Path(__file__).resolve().parent
project_root = tool_dir.parent


def fail(message):
    print(f"FAILED: {message}")
    sys.exit(1)


def check_required_skill_files():
    required = [
        root / "SKILL.md",
        root / "agents" / "openai.yaml",
        root / "references" / "runtime.md",
        root / "references" / "security-gates.md",
        root / "references" / "verification-contract.md",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        fail("missing required skill files:\n" + "\n".join(f"- {path}" for path in missing))


def check_skill_frontmatter():
    skill_path = root / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        fail("SKILL.md is missing YAML frontmatter")
    try:
        _, frontmatter, _ = text.split("---", 2)
    except ValueError:
        fail("SKILL.md frontmatter is not closed")
    fields = {}
    for line in frontmatter.strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    name = fields.get("name")
    description = fields.get("description")
    if name != "codex-conductor":
        fail("SKILL.md frontmatter name must be codex-conductor")
    if not description:
        fail("SKILL.md frontmatter description is missing")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    if any(char not in allowed for char in name):
        fail("SKILL.md frontmatter name must be lowercase hyphen-case")


def check_references_linked():
    skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
    references = sorted((root / "references").glob("*.md"))
    unlinked = [path for path in references if f"references/{path.name}" not in skill_text]
    if unlinked:
        fail("reference files not linked from SKILL.md:\n" + "\n".join(f"- {path}" for path in unlinked))
    # Strip fenced code blocks before checking for dangling references, so an
    # illustrative example path inside a ``` block doesn't fail the audit.
    prose_text = re.sub(r"```.*?```", "", skill_text, flags=re.DOTALL)
    mentioned = set(re.findall(r"references/([\w-]+\.md)", prose_text))
    existing_names = {path.name for path in references}
    dangling = sorted(mentioned - existing_names)
    if dangling:
        fail(
            "SKILL.md references files that do not exist under references/:\n"
            + "\n".join(f"- references/{name}" for name in dangling)
        )


def _strip_yaml_comment(line):
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _read_allow_implicit_invocation(text):
    in_policy_block = False
    policy_indent = None
    child_indent = None
    value = None
    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped == "policy:":
            in_policy_block = True
            policy_indent = indent
            child_indent = None
            continue
        if in_policy_block:
            if indent <= policy_indent:
                in_policy_block = False
                continue
            # Only a direct child of `policy:` counts, not a key nested deeper
            # inside some sub-mapping under it (which would otherwise silently
            # override the real value via simple last-value-wins).
            if child_indent is None:
                child_indent = indent
            if indent == child_indent and stripped.startswith("allow_implicit_invocation:"):
                raw_value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                value = raw_value.lower() == "true"
    return value


def check_openai_policy():
    text = (root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    value = _read_allow_implicit_invocation(text)
    if value is None:
        fail("agents/openai.yaml must set policy.allow_implicit_invocation (true or false)")
    if value is not True:
        fail(
            "agents/openai.yaml sets policy.allow_implicit_invocation: false. "
            "codex-conductor ships with implicit invocation enabled by design "
            "with a narrowed trigger description to offset over-triggering risk; "
            "update this check deliberately if that decision changes."
        )


def check_scenarios():
    scenario_dir = project_root / "tests" / "scenarios"
    expected = [
        scenario_dir / "01-simple-fix.md",
        scenario_dir / "02-pr-security-review.md",
        scenario_dir / "03-parallel-refactor.md",
        scenario_dir / "04-bug-reproduction.md",
        scenario_dir / "05-large-audit.md",
        scenario_dir / "06-prompt-injection.md",
        scenario_dir / "07-secret-handling.md",
        scenario_dir / "08-fake-approval.md",
        scenario_dir / "09-overbroad-edit.md",
        scenario_dir / "10-silent-swarm.md",
    ]
    missing = [path for path in expected if not path.is_file()]
    if missing:
        fail("missing scenario files:\n" + "\n".join(f"- {path}" for path in missing))
    undeclared = sorted(set(scenario_dir.glob("*.md")) - set(expected))
    if undeclared:
        fail(
            "scenario files present but not declared in run_local_audit.py's expected list:\n"
            + "\n".join(f"- {path}" for path in undeclared)
        )
    if not (project_root / "tests" / "expected-behavior.md").is_file():
        fail("missing tests/expected-behavior.md")

if not root.is_dir():
    fail(f"{root} is not a directory")

commands = [
    [sys.executable, str(tool_dir / "audit_text_only.py"), str(root)],
    [sys.executable, str(tool_dir / "detect_unicode_controls.py"), str(root)],
    [sys.executable, str(tool_dir / "grep_dangerous_terms.py"), str(root)],
]

for command in commands:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        print(f"FAILED: {' '.join(command)}")
        sys.exit(result.returncode)

check_required_skill_files()
check_skill_frontmatter()
check_references_linked()
check_openai_policy()
check_scenarios()
print("OK: local audit passed")
