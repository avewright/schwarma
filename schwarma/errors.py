"""
Structured error hierarchy for the Schwarma framework.

All exchange-facing errors derive from :class:`SchwarmaError`, which
carries a machine-readable ``code`` alongside the human-readable message.
This enables callers to programmatically distinguish error categories
without parsing message strings.
"""

from __future__ import annotations


class SchwarmaError(Exception):
    """Base class for all Schwarma exceptions.

    Attributes:
        code:    Machine-readable identifier (e.g. ``"NOT_FOUND"``).
        message: Human-readable explanation.
    """

    code: str = "SCHWARMA_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        self.message = message
        super().__init__(f"[{self.code}] {message}" if message else self.code)


# ------------------------------------------------------------------
# Lookup failures
# ------------------------------------------------------------------

class NotFoundError(SchwarmaError):
    """Raised when a referenced entity does not exist."""

    code = "NOT_FOUND"

    def __init__(self, entity: str = "entity", id_value: object = None) -> None:
        detail = f"{entity} not found"
        if id_value is not None:
            detail = f"{entity} {id_value} not found"
        super().__init__(detail)
        self.entity = entity
        self.id_value = id_value


# ------------------------------------------------------------------
# Permission / authorisation
# ------------------------------------------------------------------

class PermissionError_(SchwarmaError):
    """Raised when an agent lacks permission for the requested action.

    Named with a trailing underscore to avoid shadowing the built-in
    ``PermissionError``.  Import as ``from schwarma.errors import
    PermissionError_ as PermissionError`` if preferred.
    """

    code = "PERMISSION_DENIED"


class SuspendedError(PermissionError_):
    """Raised when a suspended agent attempts a restricted action."""

    code = "AGENT_SUSPENDED"


# ------------------------------------------------------------------
# State violations
# ------------------------------------------------------------------

class StateError(SchwarmaError):
    """Raised when an action is invalid for the current entity state."""

    code = "INVALID_STATE"


class SolverTimeoutError(StateError):
    """Raised when an agent solver exceeds its allowed execution time."""

    code = "SOLVER_TIMEOUT"


class DuplicateError(SchwarmaError):
    """Raised when an idempotency check detects a duplicate operation."""

    code = "DUPLICATE"


# ------------------------------------------------------------------
# Rate / capacity limits
# ------------------------------------------------------------------

class RateLimitError(SchwarmaError):
    """Raised when a per-agent rate limit is exceeded."""

    code = "RATE_LIMITED"


class CapacityError(SchwarmaError):
    """Raised when an agent has too many active claims."""

    code = "CAPACITY_EXCEEDED"


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

class ValidationError(SchwarmaError):
    """Raised when input data fails validation (guards, effort, etc.)."""

    code = "VALIDATION_FAILED"


class GuardBlockError(ValidationError):
    """Raised when content guards block a problem or solution."""

    code = "GUARD_BLOCKED"


# ------------------------------------------------------------------
# Calibration
# ------------------------------------------------------------------

class CalibrationError(SchwarmaError):
    """Raised for calibration-specific failures."""

    code = "CALIBRATION_ERROR"


# ------------------------------------------------------------------
# Dependency / decomposition
# ------------------------------------------------------------------

class DependencyError(SchwarmaError):
    """Raised when a problem has unmet dependencies (blocked)."""

    code = "DEPENDENCY_UNMET"


class CircularDependencyError(DependencyError):
    """Raised when adding a dependency would create a cycle."""

    code = "CIRCULAR_DEPENDENCY"
