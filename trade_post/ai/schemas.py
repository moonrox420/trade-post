"""Versioned AI output schema (PRD M4) and strict-JSON validation.

The PRD mandates that every AI "decision" is a strictly-validated JSON object
conforming to a versioned schema. Schema version ``v1`` is defined below.
Invalid outputs are rejected, never used to place orders, and recorded with
their ``rejection_reason`` for audit.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft7Validator, exceptions

#: Version tag persisted alongside every decision, and on the ai_decisions row.
SCHEMA_VERSION = "v1"

DECISION_SCHEMA_V1: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://trade-post.local/schema/ai-decision/v1",
    "title": "AIDecisionV1",
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "symbol"],
    "properties": {
        "action": {"enum": ["BUY", "SELL", "HOLD"]},
        "symbol": {"type": "string", "minLength": 1, "maxLength": 32},
        "price": {"type": ["string", "number", "null"]},
        "quantity": {"type": ["string", "number", "null"]},
        "confidence": {
            "type": ["number", "string"],
            "minimum": 0,
            "maximum": 1,
        },
        "stop_loss": {"type": ["string", "number", "null"]},
        "take_profit": {"type": ["string", "number", "null"]},
        "rationale": {"type": "string", "maxLength": 480},
    },
}

_VALIDATOR = Draft7Validator(DECISION_SCHEMA_V1)


def validate_decision(raw: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a raw AI decision against schema v1.

    Returns ``(valid, errors)`` where ``errors`` is a list of human-readable
    ``path: message`` entries. Never raises for a schema violation; callers
    branch on the returned flag.
    """
    if not isinstance(raw, dict):
        return False, ["decision must be a JSON object"]
    errors: list[str] = []
    for err in sorted(_VALIDATOR.iter_errors(raw), key=_error_sort_key):
        where = ".".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{where}: {err.message}")
    return not errors, errors


def _error_sort_key(error: exceptions.ValidationError):
    return (error.absolute_path, error.message)
