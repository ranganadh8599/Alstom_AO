"""Input and output guardrails.

The reference implementation (``multi-agent-orchestator``) uses Guardrails AI hub
validators (``DetectPII``, ``DetectJailbreak``, ``ProfanityFree``). That requires a
hub account and network access at boot, which is a poor fit for an orchestration
layer that must start on-prem. These are local, deterministic equivalents for the
cases that matter most, with the same shape so a hub validator can be swapped in.

Deliberately conservative: a guardrail that blocks legitimate business questions
is worse than no guardrail. Everything is reported and only ``block``-level
findings stop a run.

Modes (``AO_GUARDRAILS_MODE``):

``off``    no checks
``warn``   record findings, run proceeds (default - safe to roll out)
``block``  input findings reject the request; output findings are redacted
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- PII patterns -----------------------------------------------------------
# Deliberately narrow. A broad "any 9 digits" pattern matches invoice ids and
# order numbers, which is most of what this platform handles.
_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "us_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone_intl": re.compile(r"\+\d{1,3}[ -]?\d{2,4}[ -]?\d{3,4}[ -]?\d{3,4}\b"),
}

# A credential being *handled* (never "requested") is the risky case.
_SECRET_REQUEST = re.compile(
    r"\b(?:send|paste|share|give|reply with|tell|type|enter|provide|read out)\b"
    r"(?:\s+me)?\s+(?:your|the|that|this|a)?\s*"
    r"(?:password|passcode|otp|one[- ]time code|api[ _-]?key|secret key|private key|seed phrase)\b",
    re.I,
)

# Prompt-injection / instruction-hijack shapes, on *untrusted* text.
_INJECTION = [
    re.compile(r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+instructions?\b", re.I),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:your\s+)?(?:previous\s+)?(?:instructions|rules|guidelines)\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|in)\b.{0,40}\bmode\b", re.I),
    re.compile(r"\b(?:reveal|print|show|repeat)\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions)\b", re.I),
    re.compile(r"\bact\s+as\s+(?:a\s+)?(?:different|new)\s+(?:assistant|agent|model)\b", re.I),
    re.compile(r"<\s*\|?\s*(?:im_start|system|endoftext)\s*\|?\s*>", re.I),
]

_PROFANITY = re.compile(
    r"\b(?:f+u+c+k+\w*|sh[i1]t+\w*|b[i1]tch\w*|a+s+s+h+o+l+e+\w*|c+u+n+t+\w*|d[i1]ck\w*)\b",
    re.I,
)

# Findings at this level stop a run in ``block`` mode; others are advisory.
_CATEGORY_LEVEL = {
    "pii": "warn",
    "profanity": "warn",
    "secret_request": "block",
    "prompt_injection": "block",
}


@dataclass
class Finding:
    category: str
    detail: str
    level: str = "warn"

    def describe(self) -> str:
        return f"{self.category}: {self.detail}"


@dataclass
class GuardrailReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.level == "block"]

    def describe(self) -> str:
        if self.ok:
            return "clean"
        return ", ".join(finding.describe() for finding in self.findings[:5])

    def redacted_text(self, original: str) -> str:
        """Mask PII occurrences; used when an output finding must not ship."""

        text = original
        for category, pattern in _PII_PATTERNS.items():
            text = pattern.sub(f"[{category.upper()}_REDACTED]", text)
        return text


def _luhn(value: str) -> bool:
    digits = [int(char) for char in re.sub(r"\D", "", value)]
    if len(digits) < 13:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def check_input(text: str) -> GuardrailReport:
    """Untrusted user text entering the orchestration layer."""

    report = GuardrailReport()
    lowered = text or ""

    for pattern in _INJECTION:
        match = pattern.search(lowered)
        if match:
            report.findings.append(
                Finding("prompt_injection", match.group(0)[:60], level=_CATEGORY_LEVEL["prompt_injection"])
            )
            break

    match = _SECRET_REQUEST.search(lowered)
    if match:
        report.findings.append(
            Finding("secret_request", match.group(0)[:60], level=_CATEGORY_LEVEL["secret_request"])
        )

    for category, pattern in _PII_PATTERNS.items():
        for found in pattern.finditer(lowered):
            value = found.group(0)
            # Card numbers only count when they pass the checksum, otherwise every
            # long numeric id becomes "PII".
            if category == "credit_card" and not _luhn(value):
                continue
            report.findings.append(Finding("pii", f"{category} present", level=_CATEGORY_LEVEL["pii"]))
            break

    if _PROFANITY.search(lowered):
        report.findings.append(Finding("profanity", "profanity detected", level=_CATEGORY_LEVEL["profanity"]))
    return report


def check_output(text: str) -> GuardrailReport:
    """Text leaving the platform (synthesized answer or agent output)."""

    report = GuardrailReport()
    body = text or ""
    for category, pattern in _PII_PATTERNS.items():
        for found in pattern.finditer(body):
            if category == "credit_card" and not _luhn(found.group(0)):
                continue
            report.findings.append(Finding("pii", f"{category} in output", level=_CATEGORY_LEVEL["pii"]))
            break
    if _PROFANITY.search(body):
        report.findings.append(Finding("profanity", "profanity in output", level=_CATEGORY_LEVEL["profanity"]))
    return report


def apply_input_guards(text: str, mode: str) -> tuple[GuardrailReport, bool]:
    """Return ``(report, blocked)``."""

    if mode == "off":
        return GuardrailReport(), False
    report = check_input(text)
    blocked = mode == "block" and bool(report.blocking)
    return report, blocked


def apply_output_guards(text: str, mode: str) -> tuple[str, GuardrailReport]:
    """Return ``(possibly_redacted_text, report)``."""

    if mode == "off":
        return text, GuardrailReport()
    report = check_output(text)
    if mode == "block" and report.findings:
        return report.redacted_text(text), report
    return text, report
