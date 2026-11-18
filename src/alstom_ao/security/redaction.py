"""Secret redaction for anything that leaves the process or is persisted.

Two distinct surfaces need this and neither should own the pattern:

* **Traces** — span attributes and the ``trace_events`` ledger, both readable
  through the API. Prompts, tool arguments and error strings are user-controlled,
  so a pasted credential can end up there.
* **Run records** — ``query``/``response``/``error`` columns returned by
  ``/v1/runs*``.

Redaction is a safety net, not a substitute for keeping credentials out of
requests. It matches well-known key shapes and replaces them with ``[redacted]``;
it cannot detect an arbitrary opaque secret.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# Key shapes worth catching. Deliberately specific: a greedy pattern would mangle
# legitimate content such as invoice numbers or ids.
SECRET_PATTERN = re.compile(
    r"(?:sk|pk)-lf-[A-Za-z0-9-]{8,}"                                        # Langfuse
    r"|sk-[A-Za-z0-9_\-]{20,}"                                             # OpenAI-style
    r"|gh[pousr]_[A-Za-z0-9]{20,}"                                         # GitHub tokens
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"                                       # Slack tokens
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"   # JWT
    r"|Basic\s+[A-Za-z0-9+/=]{24,}"                                        # Basic auth
)


def redact_text(value: str) -> str:
    """Replace secret-shaped substrings inside a string."""

    return SECRET_PATTERN.sub(REDACTED, value) if SECRET_PATTERN.search(value) else value


def redact_secrets(value: str | None) -> str | None:
    """Convenience wrapper for nullable stored fields."""

    if value is None:
        return None
    return redact_text(str(value))


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside JSON-like structures.

    Non-string scalars pass through untouched so numbers and booleans keep their
    type in span attributes.
    """

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
