"""Contract enforcement for agent output.

Every capability card declares an ``output_schema``, and until now nothing checked
it: an agent could return anything and the synthesizer would treat it as fact. This
validates what an agent actually returned against what its card promised.

Two things are deliberately *not* errors:

* an agent that returns only free text while the card declares a JSON contract is
  reported as ``not_applicable`` for text-only payloads - A2A peers legitimately
  answer in prose, and failing them would break the common case;
* a card with no ``output_schema`` is simply unchecked (``schema_missing`` is
  advisory, not a failure).

Modes (``AO_OUTPUT_SCHEMA_MODE``):

``off``     no validation
``warn``    record the violation, keep the result
``enforce`` reject the agent result (treated as a failed agent, so the run still
            returns whatever else succeeded)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

logger = logging.getLogger(__name__)

MAX_ERRORS = 5


@dataclass
class SchemaVerdict:
    status: str  # valid | invalid | not_applicable | schema_missing | schema_invalid
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"valid", "not_applicable", "schema_missing"}

    def describe(self) -> str:
        if self.ok:
            return self.status
        return f"{self.status}: " + "; ".join(self.errors[:MAX_ERRORS])


def _candidate_payload(text: str) -> Any:
    """The JSON an agent meant to return, or ``None`` when it answered in prose."""

    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def validate_output(
    *,
    output_schema: dict[str, Any] | None,
    text: str,
    structured: dict[str, Any] | None,
) -> SchemaVerdict:
    """Validate an agent result against its declared schema.

    Preference order for the payload: the structured value the adapter extracted,
    then JSON found in the response text. Prose-only answers are not applicable.
    """

    if not output_schema:
        return SchemaVerdict(status="schema_missing")

    try:
        Draft202012Validator.check_schema(output_schema)
    except SchemaError as exc:
        # A broken card must not fail every run; report it once and move on.
        return SchemaVerdict(status="schema_invalid", errors=[exc.message[:200]])

    payload: Any = None
    if structured:
        payload = structured
    else:
        payload = _candidate_payload(text)
    if payload is None:
        return SchemaVerdict(status="not_applicable")

    validator = Draft202012Validator(output_schema)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if not errors:
        return SchemaVerdict(status="valid")
    rendered = [
        f"{'/'.join(str(part) for part in error.path) or '(root)'}: {error.message}"[:200] for error in errors
    ]
    return SchemaVerdict(status="invalid", errors=rendered)
