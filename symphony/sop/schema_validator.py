"""Node input/output schema validation for SOP nodes.

Each SOP node may declare an ``input_schema`` and ``output_schema`` (JSON
Schema, draft-07 compatible). The executor validates:

- a node's OUTPUT after it runs (does the node honor its output contract?), and
- a node's INPUT before it runs (do the upstream outputs satisfy this node's
  input contract?).

pi returns free-form assistant text in the node result's ``output`` field, so
:func:`extract_payload` first tries to parse a JSON object out of that text
(supporting both raw JSON and ```json fenced blocks). If no structured payload
can be recovered, validation against a non-empty schema fails with a clear
message so the executor can retry / escalate to a human.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

try:
    from jsonschema import Draft7Validator
    _HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover - jsonschema is a declared dependency
    _HAS_JSONSCHEMA = False


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


class SchemaValidationError(Exception):
    """Raised when a node's input or output fails schema validation."""

    def __init__(self, where: str, node_id: str, errors: list[str]):
        self.where = where  # "input" | "output"
        self.node_id = node_id
        self.errors = errors
        joined = "; ".join(errors)
        super().__init__(f"{where} schema validation failed for node '{node_id}': {joined}")


@dataclass
class ValidationResult:
    ok: bool
    payload: Any = None
    errors: list[str] = field(default_factory=list)


def extract_payload(result: Any) -> Any:
    """Best-effort extraction of a structured payload from a node result.

    Accepts either:
      - a dict node-result (``{"output": "...", ...}``) — tries ``output``,
        then the dict itself;
      - a raw string — tries to parse JSON out of it;
      - anything else — returned as-is.

    Returns the parsed object (dict/list) when JSON can be recovered, otherwise
    the original string/value.
    """
    if isinstance(result, dict):
        # Prefer an explicit structured field if the node produced one.
        if isinstance(result.get("data"), (dict, list)):
            return result["data"]
        text = result.get("output")
        if isinstance(text, (dict, list)):
            return text
        if isinstance(text, str):
            parsed = _parse_json_text(text)
            return parsed if parsed is not None else text
        # No output field — validate the dict itself (minus bookkeeping keys).
        return {k: v for k, v in result.items()
                if k not in ("status", "provider", "command_id", "skill", "tool_calls")}
    if isinstance(result, str):
        parsed = _parse_json_text(result)
        return parsed if parsed is not None else result
    return result


def _parse_json_text(text: str) -> Any | None:
    """Try to parse a JSON object/array from free-form text."""
    text = text.strip()
    if not text:
        return None
    # 1) Whole string is JSON.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) A ```json fenced block.
    m = _FENCED_JSON.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    # 3) First balanced {...} object in the text.
    obj = _first_json_object(text)
    if obj is not None:
        return obj
    return None


def _first_json_object(text: str) -> Any | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break
        start = text.find("{", start + 1)
    return None


def validate(payload: Any, schema: dict) -> ValidationResult:
    """Validate ``payload`` against a JSON Schema.

    An empty schema means "no contract" and always passes. If jsonschema is
    unavailable, validation is skipped (passes) so the pipeline never hard-fails
    on a missing optional dependency.
    """
    if not schema:
        return ValidationResult(ok=True, payload=payload)
    if not _HAS_JSONSCHEMA:  # pragma: no cover
        return ValidationResult(ok=True, payload=payload)

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if not errors:
        return ValidationResult(ok=True, payload=payload)

    msgs = []
    for e in errors:
        loc = "/".join(str(p) for p in e.path) or "<root>"
        msgs.append(f"{loc}: {e.message}")
    return ValidationResult(ok=False, payload=payload, errors=msgs)


def validate_output(node_id: str, result: Any, schema: dict) -> ValidationResult:
    """Validate a node's OUTPUT against its output_schema."""
    payload = extract_payload(result)
    return validate(payload, schema)


def validate_input(node_id: str, node_input: dict, schema: dict) -> ValidationResult:
    """Validate a node's INPUT (aggregated upstream outputs) against input_schema.

    ``node_input`` maps dependency-node-id -> that node's result. For validation
    we flatten each dependency's extracted payload so the input schema can be
    written against the upstream data directly.
    """
    if not schema:
        return ValidationResult(ok=True, payload=node_input)

    flat: dict = {}
    for dep_id, dep_result in (node_input or {}).items():
        payload = extract_payload(dep_result)
        if isinstance(payload, dict):
            # Merge dependency fields; keep dep-scoped copy too for disambiguation.
            flat.setdefault(dep_id, payload)
            for k, v in payload.items():
                flat.setdefault(k, v)
        else:
            flat[dep_id] = payload

    return validate(flat, schema)
