#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("codex-conductor")
if not root.is_dir():
    print(f"FAILED: {root} is not a directory")
    sys.exit(1)

allowed_suffixes = {".md", ".yaml", ".yml", ".txt"}
denied_suffixes = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".fish",
    ".bat", ".ps1", ".exe", ".dll", ".so", ".dylib", ".zip", ".tar",
    ".gz", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf",
    ".docx", ".xlsx", ".pptx", ".wasm", ".jar"
}
denied_names = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
errors = []

for path in root.rglob("*"):
    if not path.is_file():
        continue
    suffix = path.suffix.lower()
    if path.name in denied_names:
        errors.append(f"denied sensitive filename: {path}")
    if suffix in denied_suffixes:
        errors.append(f"denied executable/binary/archive/media suffix: {path}")
    if suffix not in allowed_suffixes:
        errors.append(f"non-allowlisted suffix: {path}")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"not valid utf-8 text: {path}")

if errors:
    print("Text-only audit failed:")
    for error in errors:
        print(f"- {error}")
    sys.exit(1)

print("OK: installable skill folder is text-only and allowlisted")
