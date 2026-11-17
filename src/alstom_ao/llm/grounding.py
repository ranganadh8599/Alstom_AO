"""Numeric grounding for synthesized answers (and agent self-consistency).

Three separate problems, three separate answers:

1. **The synthesizer invents arithmetic.** Given "25 days per calendar year" and
   "plus 5 days after three years" it produced "a total of 80 days". A prompt rule
   cannot guarantee this, so every figure in the answer is verified against what
   the agents actually reported (or what the user asked).

2. **Literal string matching is too brittle.** "4%" vs "4.0%", "25" vs "25.0",
   "1,200" vs "1200" and "three" vs "3" are the same quantity. Comparison is
   numeric, with a tolerance only wide enough for float formatting - never wide
   enough to hide a computed total.

3. **An agent can report a wrong number.** Grounding cannot detect that by
   construction: it only compares the answer with the agent. What it *can* do is
   make agent-side problems visible - cross-agent disagreement on the same metric,
   and figures in an agent's prose that its own structured payload contradicts.

Modes (``AO_SYNTHESIS_GROUNDING``):

``off``     no check
``warn``    keep the answer, append a caveat listing ungrounded figures
``strict``  repair once, then redact only the offending sentences; fall back to
            verbatim agent text only when nothing usable survives
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# A "standalone" number is not part of an identifier: no letter/digit/underscore/
# dash/slash immediately beside it. That keeps INV-2024-0912, v2.3.0, 2024-Q3 and
# PO-7781 out of the comparison, since those are labels rather than quantities.
_STANDALONE_NUMBER = re.compile(r"(?<![\w\-/.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w\-/])")

# Years are legitimate in prose ("in 2024 the policy changed") and flagging them
# produces noise. A fabricated year is a lower risk than a fabricated entitlement.
_BENIGN_RANGE = (1900.0, 2100.0)

# Spelled-out quantities that models use interchangeably with digits.
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
}
_WORD_PATTERN = re.compile(r"\b(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b", re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")

# Reference numbers are labels, not quantities: "policy 4.2", "section 3.1.4",
# "clause 12", "step 2", "version 1.2", "§ 7". Without this a policy citation looks
# like an invented figure and gets a legitimate sentence redacted.
_REFERENCE_CONTEXT = re.compile(
    r"(?:policy|section|clause|article|step|version|revision|annex|appendix|chapter|schedule|\u00a7)"
    r"\s*#?\s*$",
    re.I,
)


def _is_reference(text: str, start: int) -> bool:
    """True when the number at ``start`` is a document reference, not a quantity."""

    return bool(_REFERENCE_CONTEXT.search((text or "")[max(0, start - 24) : start]))

# Tolerance only for representation differences (4 vs 4.0 vs 4.00), never for
# genuine arithmetic: 80 vs 75 must never be considered "close".
_REL_TOL = 1e-9
_ABS_TOL = 1e-9


def _to_canonical(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def extract_numbers(text: str, *, include_words: bool = True) -> set[str]:
    """Normalized quantities in ``text``.

    ``include_words`` maps spelled-out numbers ("three years") to digits so that a
    restatement is not reported as fabricated. Document references
    ("policy 4.2", "section 3.1.4") are excluded: they are labels, not quantities.
    """

    found: set[str] = set()
    for match in _STANDALONE_NUMBER.finditer(text or ""):
        if _is_reference(text, match.start(1)):
            continue
        raw = match.group(1).replace(",", "")
        try:
            found.add(_to_canonical(float(raw)))
        except ValueError:
            continue
    if include_words:
        for match in _WORD_PATTERN.finditer(text or ""):
            if _is_reference(text, match.start(1)):
                continue
            found.add(str(_NUMBER_WORDS[match.group(1).lower()]))
    return found


def _parse(values: set[str]) -> list[float]:
    parsed: list[float] = []
    for value in values:
        try:
            parsed.append(float(value))
        except ValueError:
            continue
    return parsed


def _matches(value: float, known: list[float]) -> bool:
    return any(
        abs(value - candidate) <= max(_ABS_TOL, _REL_TOL * max(abs(value), abs(candidate)))
        for candidate in known
    )


def _benign(value: float) -> bool:
    return value.is_integer() and _BENIGN_RANGE[0] <= value <= _BENIGN_RANGE[1]


@dataclass
class GroundingReport:
    grounded: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    def describe(self) -> str:
        if self.ok:
            return "all figures grounded in agent results"
        return "ungrounded figures: " + ", ".join(sorted(self.ungrounded))


def check_grounding(answer: str, sources: list[str], *, query: str = "") -> GroundingReport:
    """Figures in ``answer`` that match no agent result and not the question."""

    known_numbers: set[str] = set()
    for text in sources:
        known_numbers |= extract_numbers(text)
    known_numbers |= extract_numbers(query)
    known = _parse(known_numbers)

    report = GroundingReport()
    for number in sorted(extract_numbers(answer), key=float):
        value = float(number)
        if _matches(value, known) or _benign(value):
            report.grounded.append(number)
        else:
            report.ungrounded.append(number)
    return report


# ---------------------------------------------------------------------------
# mode application: repair -> redact -> verbatim
# ---------------------------------------------------------------------------

CAVEAT_TEMPLATE = (
    "\n\n> ⚠️ Verification note: these figures are not present in any agent result and may have "
    "been calculated or invented: {numbers}. Treat them as unverified."
)

REDACTED_NOTE = (
    "\n\n> ⚠️ Removed {count} sentence(s) that introduced figures no agent reported ({numbers}). "
    "The remaining text quotes the agents."
)

STRICT_NOTE = (
    "\n\n> ⚠️ The synthesized answer contained figures no agent reported ({numbers}), so it was "
    "withheld. The agent output above is quoted verbatim instead."
)


def repair_prompt(*, answer: str, ungrounded: list[str], sources: list[str]) -> str:
    """Ask the model to rewrite without inventing figures (one extra call)."""

    return (
        "Your previous answer used figures no agent reported: "
        + ", ".join(ungrounded)
        + ".\n\nRewrite the answer so every number appears verbatim in the agent output below. "
        "Do not add, total, average, annualise or extrapolate anything, and do not mention this "
        "instruction.\n\n"
        f"Previous answer:\n{answer}\n\n"
        "Agent output:\n" + "\n\n".join(sources)
    )


def redact_sentences(answer: str, *, known: list[float]) -> tuple[str, list[str], int]:
    """Drop only the sentences carrying ungrounded figures."""

    kept: list[str] = []
    removed: list[str] = []
    removed_count = 0
    for sentence in _SENTENCE_SPLIT.split(answer or ""):
        if not sentence or not sentence.strip():
            continue
        bad = [
            number
            for number in extract_numbers(sentence)
            if not _matches(float(number), known) and not _benign(float(number))
        ]
        if bad:
            removed.extend(bad)
            removed_count += 1
        else:
            kept.append(sentence.strip())
    return " ".join(kept), sorted(set(removed)), removed_count


def _looks_like_answer(text: str) -> bool:
    """Reject empty or degenerate repairs (``{}``, ``N/A``) instead of accepting them."""

    stripped = (text or "").strip()
    if len(stripped) < 40:
        return False
    return any(char.isalpha() for char in stripped)


def apply_grounding(
    *,
    answer: str,
    results: list[str],
    query: str,
    mode: str,
    fallback: str,
    repaired_answer: str | None = None,
) -> tuple[str, GroundingReport, str | None]:
    """Return ``(final_answer, report, action)``.

    ``repaired_answer`` is the output of one repair attempt made by the caller. A
    repair that is degenerate or still ungrounded is discarded and the original
    answer is evaluated instead - a bad repair must never improve the verdict.
    ``fallback`` must be verbatim agent text.
    """

    if repaired_answer:
        repair_report = check_grounding(repaired_answer, results, query=query)
        if repair_report.ok and _looks_like_answer(repaired_answer):
            return repaired_answer, repair_report, "repaired"

    candidate = answer
    report = check_grounding(candidate, results, query=query)
    if mode == "off" or report.ok:
        return candidate, report, None

    numbers = ", ".join(sorted(report.ungrounded))
    if mode == "warn":
        return candidate + CAVEAT_TEMPLATE.format(numbers=numbers), report, "warned"

    # strict: a redacted answer is better than discarding everything.
    known_numbers: set[str] = set()
    for text in [*results, query]:
        known_numbers |= extract_numbers(text)
    redacted, removed, count = redact_sentences(candidate, known=_parse(known_numbers))
    if redacted.strip():
        return (
            redacted + REDACTED_NOTE.format(count=count, numbers=", ".join(removed) or numbers),
            report,
            "strict_redacted",
        )
    return fallback.rstrip() + STRICT_NOTE.format(numbers=numbers), report, "strict_withheld"


# ---------------------------------------------------------------------------
# agent-side visibility (the limitation grounding cannot fix by itself)
# ---------------------------------------------------------------------------


@dataclass
class AgentCheck:
    """Problems found inside agent output itself."""

    text_contradicts_structured: list[str] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.text_contradicts_structured and not self.disagreements

    def describe(self) -> str:
        parts = []
        if self.text_contradicts_structured:
            parts.append(
                "prose figures absent from structured output: " + ", ".join(self.text_contradicts_structured)
            )
        if self.disagreements:
            parts.append(f"{len(self.disagreements)} cross-agent disagreement(s)")
        return "; ".join(parts) or "agents consistent"


def numeric_fields(structured: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """Flatten numeric leaves of a structured payload into ``dotted.key -> value``."""

    out: dict[str, float] = {}
    for key, value in (structured or {}).items():
        name = f"{prefix}{key}"
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[name] = float(value)
        elif isinstance(value, dict):
            out.update(numeric_fields(value, f"{name}."))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    out.update(numeric_fields(item, f"{name}.{index}."))
    return out


def check_agent_consistency(results: list[dict[str, Any]], texts: dict[str, str]) -> AgentCheck:
    """Detect agent-internal contradictions and cross-agent disagreement.

    Only meaningful when agents return structured numbers: most enterprise agents
    answer in prose, and flagging every prose figure would be noise.
    """

    check = AgentCheck()
    metrics_by_agent: dict[str, dict[str, float]] = {}

    for item in results:
        agent_id = str(item.get("agent_id") or "unknown")
        structured = item.get("structured") or {}
        numbers = numeric_fields(structured)
        if not numbers:
            continue
        metrics_by_agent[agent_id] = numbers
        text = texts.get(agent_id, "")
        if not text:
            continue
        reported = list(numbers.values()) + _parse(extract_numbers(json.dumps(structured, default=str)))
        for number in extract_numbers(text):
            value = float(number)
            if _benign(value) or _matches(value, reported):
                continue
            check.text_contradicts_structured.append(f"{agent_id}:{number}")

    # Same metric name, different value across agents: the platform cannot pick a
    # winner, so the disagreement is surfaced rather than silently trusted.
    names: dict[str, dict[str, float]] = {}
    for agent_id, metrics in metrics_by_agent.items():
        for name, value in metrics.items():
            names.setdefault(name, {})[agent_id] = value
    for name, by_agent in names.items():
        if len(by_agent) < 2:
            continue
        # Compare each value against the *other* agents' values. Checking against the
        # full list would always match a value with itself and hide the conflict.
        conflicting = False
        for agent_id, value in by_agent.items():
            others = [other for other_id, other in by_agent.items() if other_id != agent_id]
            if not _matches(value, others):
                conflicting = True
                break
        if conflicting:
            check.disagreements.append({"metric": name, "agents": by_agent})
    return check
