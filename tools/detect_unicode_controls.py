#!/usr/bin/env python3
from pathlib import Path
import sys
import unicodedata

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("codex-conductor")
if not root.is_dir():
    print(f"FAILED: {root} is not a directory")
    sys.exit(1)

allowed = {"\n", "\r", "\t"}
# Known invisible/steganographic ranges used to smuggle hidden text (e.g. variation
# selectors, the Tags block) that unicodedata.category() does not classify as Cf/Cc.
suspicious_ranges = (
    (0x200B, 0x200F),   # zero-width space/joiners, LRM/RLM
    (0x2060, 0x2064),   # word joiner and invisible math operators
    (0xFE00, 0xFE0F),   # Variation Selectors
    (0xE0000, 0xE007F), # Tags block
    (0xE0100, 0xE01EF), # Variation Selectors Supplement
)
findings = []


def is_suspicious(char):
    category = unicodedata.category(char)
    if category in {"Cf", "Cc"} and char not in allowed:
        return True
    code = ord(char)
    return any(start <= code <= end for start, end in suspicious_ranges)


for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        findings.append((str(path), "-", "BINARY_OR_NON_UTF8", "file is not valid utf-8"))
        continue
    for index, char in enumerate(text):
        if is_suspicious(char):
            findings.append((str(path), index, f"U+{ord(char):04X}", unicodedata.name(char, "UNKNOWN")))

if findings:
    print("Suspicious Unicode/control characters found:")
    for item in findings:
        print("\t".join(map(str, item)))
    sys.exit(1)

print("OK: no suspicious Unicode/control characters found")
