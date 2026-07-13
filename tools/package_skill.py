#!/usr/bin/env python3
from pathlib import Path
import sys
import zipfile

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("codex-conductor")
dist = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("dist")
output = dist / "skill.zip"

if not root.is_dir():
    print(f"Package failed: {root} is not a directory")
    sys.exit(1)

root_name = root.resolve().name
allowed = {
    root / "SKILL.md",
    root / "agents" / "openai.yaml",
}

allowed.update((root / "references").glob("*.md"))

missing = [path for path in sorted(allowed) if not path.is_file()]
if missing:
    print("Package failed: required files are missing:")
    for path in missing:
        print(f"- {path}")
    sys.exit(1)

dist.mkdir(parents=True, exist_ok=True)
if output.exists():
    output.unlink()

with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(allowed):
        # Always arcname relative to a single top-level folder, regardless of
        # whether `root` was passed as relative, absolute, or with a trailing slash.
        arcname = f"{root_name}/{path.relative_to(root).as_posix()}"
        archive.write(path, arcname)

size = output.stat().st_size
limit = 25 * 1024 * 1024
if size >= limit:
    print(f"Package failed: {output} is {size} bytes, limit is under {limit} bytes")
    sys.exit(1)

print(f"OK: wrote {output} ({size} bytes)")
