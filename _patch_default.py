"""Throwaway: default repository.insert_ai_decision schema_version to 'v1'."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PATH = Path("trade_post/persistence/repository.py")
text = PATH.read_text(encoding="utf-8")

old_sig = (
    "model, prompt_version, schema_version,\n"
    "                                validated, validation_errors, timestamp, trace_id) -> None:\n"
)
new_sig = (
    "model, prompt_version, schema_version=\"v1\",\n"
    "                                validated, validation_errors, timestamp, trace_id) -> None:\n"
)
if old_sig not in text:
    raise SystemExit("signature anchor not found")
text = text.replace(old_sig, new_sig, 1)

fd, tmp_path = tempfile.mkstemp(dir=str(PATH.parent), prefix=".repo_tmp_", suffix=".py")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp_path, PATH)
except Exception:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    raise

print("schema_version defaulted to 'v1'")