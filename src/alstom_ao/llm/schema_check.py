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

# JSON Schema keywords that can pull in another document. A remote value makes the
# validation result depend on a network fetch: OPA dereferences `$ref`s at
# evaluation time, and its builtins restrict fetching to the `allow_net`
# capability. Either way the outcome stops being a function of the payload alone.
_REF_KEYS = ("$ref", "$dynamicRef")


def find_external_refs(schema: Any, path: str = "") -> list[str]:
    """Every `$ref`/`$dynamicRef` value that is not a local pointer.

    Local pointers (``#/$defs/X``) are safe: they resolve inside the same document.
    Anything else (``https://…``, ``other.json#/x``) requires a fetch.
    """

    found: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            here = f"{path}/{key}" if path else key
            if key in _REF_KEYS and isinstance(value, str) and not value.startswith("#"):
                found.append(f"{here}={value[:120]}")
            else:
                found.extend(find_external_refs(value, here))
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            found.extend(find_external_refs(item, f"{path}/{index}"))
    return found


def validate_schema_definition(
    schema: dict[str, Any] | None,
    *,
    allow_remote_refs: bool = False,
) -> SchemaVerdict:
    """Validate a card's schema at *registration* time.

    Catching this here is the point: a malformed schema or a remote ``$ref`` is
    rejected once, with a clear message to the administrator who submitted it,
    instead of surfacing as a runtime validation failure on every agent call.
    """

    if not schema:
        return SchemaVerdict(status="schema_missing")

    if not allow_remote_refs:
        external = find_external_refs(schema)
        if external:
            return SchemaVerdict(
                status="schema_invalid",
                errors=[
                    "external $ref is not allowed (no network fetch during validation): " + ", ".join(external[:3])
                ],
            )

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return SchemaVerdict(status="schema_invalid", errors=[exc.message[:200]])
    return SchemaVerdict(status="valid")


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

    The schema was already checked at registration (``validate_schema_definition``),
    but cards can also be written directly to the database, so this re-verifies.
    """

    if not output_schema:
        return SchemaVerdict(status="schema_missing")

    try:
        Draft202012Validator.check_schema(output_schema)
    except SchemaError as exc:
        # A broken card must not fail every run; report it and move on.
        return SchemaVerdict(status="schema_invalid", errors=[exc.message[:200]])

    external = find_external_refs(output_schema)
    if external:
        # Never resolve a remote $ref at runtime: the verdict would depend on what
        # that URL serves. Registration rejects these; this is the second line.
        return SchemaVerdict(
            status="schema_invalid",
            errors=["external $ref cannot be resolved at runtime: " + ", ".join(external[:3])],
        )

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
