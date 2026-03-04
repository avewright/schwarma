"""
Content guards — automatic scanning for sensitive patterns and low-effort content.

Guards run on problem descriptions and solution bodies *before* they are
accepted into the exchange.  A guard returns a ``GuardResult`` indicating
whether the content is clean, flagged (needs human/operator review), or
blocked outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Sequence


class GuardAction(IntEnum):
    """What should happen to content that fails a guard."""

    PASS = auto()      # Content is fine
    FLAG = auto()      # Content is suspicious — hold for review
    BLOCK = auto()     # Content is clearly bad — reject immediately


@dataclass(frozen=True)
class GuardResult:
    action: GuardAction
    reasons: tuple[str, ...] = ()  # human-readable reasons

    @property
    def ok(self) -> bool:
        return self.action == GuardAction.PASS

    def __str__(self) -> str:
        if self.ok:
            return "PASS"
        return f"{self.action.name}: {'; '.join(self.reasons)}"

    @staticmethod
    def passed() -> GuardResult:
        return GuardResult(action=GuardAction.PASS)

    @staticmethod
    def flagged(*reasons: str) -> GuardResult:
        return GuardResult(action=GuardAction.FLAG, reasons=tuple(reasons))

    @staticmethod
    def blocked(*reasons: str) -> GuardResult:
        return GuardResult(action=GuardAction.BLOCK, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Built-in pattern detectors
# ---------------------------------------------------------------------------

# Each pattern: (compiled regex, label, action)
_SENSITIVE_PATTERNS: list[tuple[re.Pattern, str, GuardAction]] = [
    # API keys / tokens
    (re.compile(
        r"""(?:api[_-]?key|api[_-]?secret|token|bearer|authorization)\s*[:=]\s*['"]?[A-Za-z0-9\-_]{20,}""",
        re.IGNORECASE,
    ), "Possible API key or token", GuardAction.BLOCK),

    # AWS-style keys
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key", GuardAction.BLOCK),

    # Generic long hex/base64 secrets
    (re.compile(
        r"""(?:secret|password|passwd|pwd)\s*[:=]\s*['"]?[^\s'"]{16,}""",
        re.IGNORECASE,
    ), "Possible password or secret", GuardAction.BLOCK),

    # Email addresses (PII)
    (re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    ), "Email address detected", GuardAction.FLAG),

    # US Social Security Numbers
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "Possible SSN", GuardAction.BLOCK),

    # Credit card numbers (basic Luhn-plausible patterns)
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "Possible credit card number", GuardAction.FLAG),

    # Private keys
    (re.compile(
        r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----",
    ), "Private key material", GuardAction.BLOCK),

    # Connection strings
    (re.compile(
        r"(?:mongodb|postgres|mysql|redis|amqp)://[^\s]+:[^\s]+@",
        re.IGNORECASE,
    ), "Database connection string with credentials", GuardAction.BLOCK),
]


def scan_for_secrets(text: str) -> GuardResult:
    """Scan *text* for sensitive patterns (keys, PII, credentials)."""
    reasons: list[str] = []
    worst = GuardAction.PASS
    for pattern, label, action in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            reasons.append(label)
            if action.value > worst.value:
                worst = action
    if worst == GuardAction.PASS:
        return GuardResult.passed()
    if worst == GuardAction.BLOCK:
        return GuardResult.blocked(*reasons)
    return GuardResult.flagged(*reasons)


# ---------------------------------------------------------------------------
# Solution quality / effort guards
# ---------------------------------------------------------------------------

@dataclass
class QualityConfig:
    """Tuneable thresholds for solution quality checks."""

    min_length: int = 20             # characters — anything shorter is suspicious
    max_repetition_ratio: float = 0.6  # if >60% of chars are the same, flag it
    min_unique_words: int = 3        # need at least this many distinct words


def check_solution_effort(text: str, config: QualityConfig | None = None) -> GuardResult:
    """Check whether a solution body meets minimum effort thresholds."""
    cfg = config or QualityConfig()
    reasons: list[str] = []

    stripped = text.strip()
    if len(stripped) < cfg.min_length:
        reasons.append(f"Too short ({len(stripped)} chars, min {cfg.min_length})")

    if stripped:
        # Repetition check: most common char as fraction of total
        from collections import Counter
        counts = Counter(stripped.lower())
        most_common_ratio = counts.most_common(1)[0][1] / len(stripped)
        if most_common_ratio > cfg.max_repetition_ratio:
            reasons.append(f"Highly repetitive content ({most_common_ratio:.0%} same char)")

    words = set(stripped.lower().split())
    if len(words) < cfg.min_unique_words:
        reasons.append(f"Too few unique words ({len(words)}, min {cfg.min_unique_words})")

    if reasons:
        return GuardResult.flagged(*reasons)
    return GuardResult.passed()


# ---------------------------------------------------------------------------
# Composite guard runner
# ---------------------------------------------------------------------------

def run_guards(
    text: str,
    *,
    check_secrets: bool = True,
    check_effort: bool = False,
    block_flagged: bool = False,
    quality_config: QualityConfig | None = None,
) -> GuardResult:
    """Run all applicable guards and return the worst result."""
    results: list[GuardResult] = []

    if check_secrets:
        results.append(scan_for_secrets(text))
    if check_effort:
        results.append(check_solution_effort(text, quality_config))

    if not results:
        return GuardResult.passed()

    # Merge: worst action wins, all reasons collected
    worst_action = max(r.action for r in results)  # BLOCK > FLAG > PASS
    all_reasons = []
    for r in results:
        all_reasons.extend(r.reasons)

    if worst_action == GuardAction.PASS:
        return GuardResult.passed()
    if block_flagged and worst_action == GuardAction.FLAG:
        return GuardResult.blocked(*all_reasons)
    if worst_action == GuardAction.BLOCK:
        return GuardResult.blocked(*all_reasons)
    return GuardResult.flagged(*all_reasons)


# ---------------------------------------------------------------------------
# Redaction utility
# ---------------------------------------------------------------------------

def redact_secrets(text: str, placeholder: str = "[REDACTED]") -> str:
    """Replace detected sensitive patterns with *placeholder*.

    This is a best-effort utility — it won't catch everything, but it
    removes the most obvious exposures.
    """
    result = text
    for pattern, _label, _action in _SENSITIVE_PATTERNS:
        result = pattern.sub(placeholder, result)
    return result
