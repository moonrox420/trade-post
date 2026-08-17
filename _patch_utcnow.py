"""Throwaway: replace datetime.utcnow() with datetime.now(UTC) in repository.py."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PATH = Path("trade_post/persistence/repository.py")
text = PATH.read_text(encoding="utf-8")

replacements = [
    ("from datetime import datetime\n", "from datetime import UTC, datetime\n"),
    ("return datetime.utcnow().isoformat()", "return datetime.now(UTC).isoformat()"),
    ("< datetime.utcnow():", "< datetime.now(UTC):"),
]
for old, new in replacements:
    if old not in text:
        print(f"WARN: anchor not found: {old!r}")
        continue
    text = text.replace(old, new, 1)

fd, tmp_path = tempfile.mkstemp(dir=str(PATH.parent), prefix=".repo_tmp_", suffix=".py")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp_path, PATH)
except Exception:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    raise

print("repository.py utcnow -> now(UTC) patched")
