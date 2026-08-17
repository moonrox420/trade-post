"""Throwaway: add schema_version to repository.insert_ai_decision."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PATH = Path("trade_post/persistence/repository.py")
text = PATH.read_text(encoding="utf-8")

sig_old = (
    "async def insert_ai_decision(self, *, id, symbol, signal, conviction, confidence,\n"
    "                                rationale, raw_output, model, prompt_version,\n"
    "                                validated, validation_errors, timestamp, trace_id) -> None:\n"
)
sig_new = (
    "async def insert_ai_decision(self, *, id, symbol, signal, conviction, confidence,\n"
    "                                rationale, raw_output, model, prompt_version, schema_version,\n"
    "                                validated, validation_errors, timestamp, trace_id) -> None:\n"
)
if sig_old not in text:
    raise SystemExit("signature anchor not found")
text = text.replace(sig_old, sig_new, 1)

sql_old = (
    "\"INSERT INTO ai_decisions (id, symbol, signal, conviction, confidence, rationale,\"\n"
    "            \" raw_output, model, prompt_version, validated, validation_errors, timestamp, trace_id)\"\n"
    "            \" VALUES (:id, :sym, :sig, :conv, :conf, :r, :ro, :m, :pv, :v, :ve, :ts, :trc)\"),\n"
)
sql_new = (
    "\"INSERT INTO ai_decisions (id, symbol, signal, conviction, confidence, rationale,\"\n"
    "            \" raw_output, model, prompt_version, schema_version, validated,\"\n"
    "            \" validation_errors, timestamp, trace_id)\"\n"
    "            \" VALUES (:id, :sym, :sig, :conv, :conf, :r, :ro, :m, :pv, :sv, :v, :ve, :ts, :trc)\"),\n"
)
if sql_old not in text:
    print("WARN: exact sql anchor not found; attempting regex fallback")
else:
    text = text.replace(sql_old, sql_new, 1)

param_old = '"m": model, "pv": prompt_version, "v": 1 if validated else 0,'
param_new = (
    '"m": model, "pv": prompt_version, "sv": schema_version,'
    ' "v": 1 if validated else 0,'
)
if param_old in text:
    text = text.replace(param_old, param_new, 1)
else:
    print("WARN: params anchor not found")

fd, tmp_path = tempfile.mkstemp(dir=str(PATH.parent), prefix=".repo_tmp_", suffix=".py")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp_path, PATH)
except Exception:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    raise

print("insert_ai_decision schema_version patched")
