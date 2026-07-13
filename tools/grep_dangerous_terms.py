#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("codex-conductor")
if not root.is_dir():
    print(f"FAILED: {root} is not a directory")
    sys.exit(1)

patterns = [
    r"(curl|wget)\s+.*\|\s*((sudo|env|exec)\s+)*(\S*/)?\b(sh|bash|zsh|dash|ksh|fish)\b",
    r"danger-full-access",
    r"subprocess",
    r"eval\(",
    r"exec\(",
    r"API_KEY\s*=",
    r"SECRET\s*=",
    r"PRIVATE KEY",
]
compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

# Command-boundary characters: shell separators, subshell/command-substitution
# delimiters, and backticks. Splitting on these (rather than requiring a fixed
# whitelist of characters immediately before "rm") means `\rm` (alias bypass),
# `$(rm ...)`, and backtick-wrapped invocations still isolate their own command
# segment instead of being ignored or merged with a neighboring command.
command_boundary = re.compile(r"[;&|`]|\$\(|\)")
# Matched with search(), not match(): "rm" can legitimately appear after prose
# on the same segment (a bullet, "Never do:", a numbered step, "Then run ..."),
# which is the normal way this tool's actual corpus -- markdown docs -- mentions
# a command. `(?:^|\W)` requires "rm" to start a real word, so it still can't
# match inside another word like "confirm" or "term".
rm_start = re.compile(r"(?:^|\W)(sudo\s+)?rm\b")


def rm_recursive_force(line):
    """Catch `rm` invocations combining a recursive flag and a force flag in any
    order or spelling (-rf, -fr, -r -f, --recursive --force, sudo rm -R -f,
    \\rm -rf, $(rm -rf ...), `rm -rf ...`, or rm mentioned in prose/bullets/
    numbered steps), scoped to the command-boundary segment containing the rm
    mention so an unrelated command sharing the line can't supply the flags.

    Known limitation: cannot catch a flag supplied indirectly via a separate
    process's output (e.g. `echo -r | xargs rm -f`) -- detecting that requires
    simulating shell word-splitting, out of scope for a static line scanner."""
    for raw_segment in command_boundary.split(line):
        segment = re.sub(r"\\(rm\b)", r"\1", raw_segment)
        if not rm_start.search(segment):
            continue
        tokens = re.findall(r"--?[A-Za-z-]+", segment)
        has_recursive = any(
            tok == "--recursive" or (tok.startswith("-") and not tok.startswith("--") and "r" in tok[1:].lower())
            for tok in tokens
        )
        has_force = any(
            tok == "--force" or (tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:].lower())
            for tok in tokens
        )
        if has_recursive and has_force:
            return True
    return False


findings = []

for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for line_no, line in enumerate(text.splitlines(), 1):
        for pattern in compiled:
            if pattern.search(line):
                findings.append((path, line_no, pattern.pattern, line.strip()))
        if rm_recursive_force(line):
            findings.append((path, line_no, "rm recursive+force (any flag order/spelling)", line.strip()))

if findings:
    print("Potentially dangerous terms found. Review manually:")
    for path, line_no, pattern, line in findings:
        print(f"{path}:{line_no}: {pattern}: {line}")
    sys.exit(1)

print("OK: no dangerous term matches")
