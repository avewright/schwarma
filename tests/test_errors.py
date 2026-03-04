"""Tests for the structured error hierarchy (errors.py)."""

import pytest

from schwarma.errors import (
    CalibrationError,
    CapacityError,
    CircularDependencyError,
    DependencyError,
    DuplicateError,
    GuardBlockError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    SchwarmaError,
    StateError,
    SuspendedError,
    ValidationError,
)


class TestErrorHierarchy:
    """Every error type inherits from SchwarmaError."""

    def test_base_class(self):
        err = SchwarmaError("boom")
        assert isinstance(err, Exception)
        assert err.code == "SCHWARMA_ERROR"
        assert err.message == "boom"
        assert "SCHWARMA_ERROR" in str(err)

    def test_not_found_error(self):
        err = NotFoundError("agent", "abc-123")
        assert isinstance(err, SchwarmaError)
        assert err.code == "NOT_FOUND"
        assert err.entity == "agent"
        assert err.id_value == "abc-123"
        assert "agent abc-123 not found" in str(err)

    def test_not_found_without_id(self):
        err = NotFoundError("problem")
        assert "problem not found" in str(err)

    def test_permission_error(self):
        err = PermissionError_("not allowed")
        assert isinstance(err, SchwarmaError)
        assert err.code == "PERMISSION_DENIED"

    def test_suspended_is_permission(self):
        err = SuspendedError("agent is suspended")
        assert isinstance(err, PermissionError_)
        assert isinstance(err, SchwarmaError)
        assert err.code == "AGENT_SUSPENDED"

    def test_state_error(self):
        err = StateError("problem is closed")
        assert err.code == "INVALID_STATE"

    def test_duplicate_error(self):
        err = DuplicateError("already registered")
        assert err.code == "DUPLICATE"

    def test_rate_limit_error(self):
        err = RateLimitError("too fast")
        assert err.code == "RATE_LIMITED"

    def test_capacity_error(self):
        err = CapacityError("at concurrency limit")
        assert err.code == "CAPACITY_EXCEEDED"

    def test_validation_error(self):
        err = ValidationError("bad input")
        assert err.code == "VALIDATION_FAILED"

    def test_guard_block_is_validation(self):
        err = GuardBlockError("secret detected")
        assert isinstance(err, ValidationError)
        assert isinstance(err, SchwarmaError)
        assert err.code == "GUARD_BLOCKED"

    def test_calibration_error(self):
        err = CalibrationError("bank empty")
        assert err.code == "CALIBRATION_ERROR"

    def test_dependency_error(self):
        err = DependencyError("unmet dependency")
        assert err.code == "DEPENDENCY_UNMET"

    def test_circular_dependency_is_dependency(self):
        err = CircularDependencyError("A → B → A")
        assert isinstance(err, DependencyError)
        assert isinstance(err, SchwarmaError)
        assert err.code == "CIRCULAR_DEPENDENCY"

    def test_custom_code_override(self):
        err = SchwarmaError("custom", code="MY_CODE")
        assert err.code == "MY_CODE"

    def test_all_catchable_by_base(self):
        """Every error type can be caught with `except SchwarmaError`."""
        error_types = [
            NotFoundError("x"),
            PermissionError_("x"),
            SuspendedError("x"),
            StateError("x"),
            DuplicateError("x"),
            RateLimitError("x"),
            CapacityError("x"),
            ValidationError("x"),
            GuardBlockError("x"),
            CalibrationError("x"),
            DependencyError("x"),
            CircularDependencyError("x"),
        ]
        for err in error_types:
            with pytest.raises(SchwarmaError):
                raise err
