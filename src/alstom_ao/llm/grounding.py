"""Numeric grounding for synthesized answers.

The synthesizer is asked not to invent facts, but numeric aggregation slips through:
given "25 days per year" and "plus 5 days after three years" it produced
"a total of 80 days" — arithmetic the agent never stated.

A prompt rule alone cannot be trusted for this, so the check is deterministic:
every standalone number in the synthesized answer must already appear in the
agent results (or in the user's own question). Anything else is ungrounded.

Modes (``AO_SYNTHESIS_GROUNDING``):

``off``     no check (previous behaviour)
``warn``    keep the answer, append a visible caveat listing ungrounded figures
``strict``  replace the answer with verbatim agent output (cannot be wrong) and say why

Ungrounded numbers are reported either way, so ``warn`` is safe for a rollout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A "standalone" number is not part of an identifier: no letter/digit/underscore/
# dash/slash immediately beside it. That keeps INV-2024-0912, v2.3.0, 2024-Q3 and
# PO-7781 out of the comparison, since those are labels rather than quantities.
_STANDALONE_NUMBER = re.compile(r"(?<![\w\-/.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w\-/])")

# Years are legitimate in prose ("in 2024 the policy changed") and flagging them
# produces noise. A fabricated year is a lower risk than a fabricated entitlement.
_BENIGN_RANGE = (1900.0, 2100.0)


def extract_numbers(text: str) -> set[str]:
    """Normalized numbers in ``text`` (commas removed, trailing .0 dropped)."""

    found: set[str] = set()
    for match in _STANDALONE_NUMBER.finditer(text or ""):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if value.is_integer():
            found.add(str(int(value)))
        else:
            found.add(f"{value:g}")
    return found


@dataclass
class GroundingReport:
    grounded: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    def describe(self) -> str:
        if self.ok:
            return "all numbers grounded in agent results"
        return "ungrounded figures: " + ", ".join(sorted(self.ungrounded))


def check_grounding(answer: str, sources: list[str], *, query: str = "") -> GroundingReport:
    """Numbers in ``answer`` that appear in no agent result and not in the query."""

    known = set()
    for text in sources:
        known |= extract_numbers(text)
    known |= extract_numbers(query)

    report = GroundingReport()
    for number in extract_numbers(answer):
        value = float(number)
        if number in known:
            report.grounded.append(number)
        elif _BENIGN_RANGE[0] <= value <= _BENIGN_RANGE[1] and value.is_integer():
            # A bare year is not a quantity; do not flag it.
            report.grounded.append(number)
        else:
            report.ungrounded.append(number)
    return report


CAVEAT_TEMPLATE = (
    "\n\n> ⚠️ Verification note: these figures in the answer are not present in any agent result "
    "and may have been calculated or invented: {numbers}. Treat them as unverified."
)

STRICT_NOTE = (
    "\n\n> ⚠️ The synthesized answer contained figures no agent reported ({numbers}), so it was "
    "withheld. The agent output above is quoted verbatim instead."
)


def apply_grounding(
    *,
    answer: str,
    results: list[str],
    query: str,
    mode: str,
    fallback: str,
) -> tuple[str, GroundingReport, str | None]:
    """Return ``(answer, report, note)`` after applying the configured mode.

    ``fallback`` must be verbatim agent text, used by ``strict`` mode.
    """

    report = check_grounding(answer, results, query=query)
    if mode == "off" or report.ok:
        return answer, report, None

    numbers = ", ".join(sorted(report.ungrounded))
    if mode == "strict":
        return fallback.rstrip() + STRICT_NOTE.format(numbers=numbers), report, "strict_withheld"
    return answer + CAVEAT_TEMPLATE.format(numbers=numbers), report, "warned"
