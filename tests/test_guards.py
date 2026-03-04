"""Tests for content guards — secret scanning, quality checks, redaction."""

from schwarma.guards import (
    GuardAction,
    GuardResult,
    QualityConfig,
    check_solution_effort,
    redact_secrets,
    run_guards,
    scan_for_secrets,
)


class TestSecretScanning:
    def test_clean_text_passes(self):
        result = scan_for_secrets("This is a perfectly normal problem description.")
        assert result.ok

    def test_api_key_blocked(self):
        text = "Use api_key = 'xk_test_ABCDEFGHIJKLMNOPQRSTUVWXYZ' to authenticate."
        result = scan_for_secrets(text)
        assert result.action == GuardAction.BLOCK
        assert any("API key" in r or "token" in r for r in result.reasons)

    def test_aws_key_blocked(self):
        text = "My access key is AKIAIOSFODNN7EXAMPLE"
        result = scan_for_secrets(text)
        assert result.action == GuardAction.BLOCK
        assert any("AWS" in r for r in result.reasons)

    def test_private_key_blocked(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        result = scan_for_secrets(text)
        assert result.action == GuardAction.BLOCK

    def test_connection_string_blocked(self):
        text = "Connect to postgres://admin:supersecret@db.example.com:5432/mydb"
        result = scan_for_secrets(text)
        assert result.action == GuardAction.BLOCK

    def test_ssn_blocked(self):
        text = "My SSN is 123-45-6789"
        result = scan_for_secrets(text)
        assert result.action == GuardAction.BLOCK

    def test_email_flagged(self):
        text = "Contact me at user@example.com for details."
        result = scan_for_secrets(text)
        assert result.action == GuardAction.FLAG

    def test_password_in_code_blocked(self):
        text = 'password = "MyS3cretP@ssw0rdHere1234"'
        result = scan_for_secrets(text)
        assert result.action == GuardAction.BLOCK


class TestQualityChecks:
    def test_good_solution_passes(self):
        text = "Here is a detailed solution explaining the algorithm step by step."
        result = check_solution_effort(text)
        assert result.ok

    def test_too_short_flagged(self):
        result = check_solution_effort("yes")
        assert not result.ok
        assert any("short" in r.lower() for r in result.reasons)

    def test_repetitive_flagged(self):
        text = "aaaaaaaaaaaaaaaaaaaaaaaaa"  # 25 chars, all same
        result = check_solution_effort(text)
        assert not result.ok
        assert any("repetitive" in r.lower() for r in result.reasons)

    def test_too_few_words_flagged(self):
        result = check_solution_effort("ok ok ok ok ok ok ok ok ok")
        assert not result.ok
        # Only 1 unique word
        assert any("unique words" in r.lower() for r in result.reasons)

    def test_custom_config(self):
        config = QualityConfig(min_length=5, min_unique_words=1, max_repetition_ratio=0.9)
        result = check_solution_effort("hello world", config)
        assert result.ok

    def test_empty_string(self):
        result = check_solution_effort("")
        assert not result.ok


class TestRunGuards:
    def test_clean_content_passes(self):
        result = run_guards(
            "A well-written solution with enough detail to be useful.",
            check_secrets=True,
            check_effort=True,
        )
        assert result.ok

    def test_secret_blocks_even_with_good_effort(self):
        text = "Great solution! api_key='xk_test_ABCDEFGHIJKLMNOPQRSTUVWXYZ'"
        result = run_guards(text, check_secrets=True, check_effort=True)
        assert result.action == GuardAction.BLOCK

    def test_effort_flag_without_secrets(self):
        result = run_guards("no", check_secrets=True, check_effort=True)
        assert result.action == GuardAction.FLAG

    def test_no_checks_yields_pass(self):
        result = run_guards("anything", check_secrets=False, check_effort=False)
        assert result.ok


class TestRedaction:
    def test_redacts_api_key(self):
        text = "Use api_key = 'xk_test_ABCDEFGHIJKLMNOPQRSTUVWXYZ'"
        redacted = redact_secrets(text)
        assert "xk_test" not in redacted
        assert "[REDACTED]" in redacted

    def test_redacts_aws_key(self):
        text = "key is AKIAIOSFODNN7EXAMPLE"
        redacted = redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted

    def test_custom_placeholder(self):
        text = "secret = 'abc123def456ghi789'"
        redacted = redact_secrets(text, placeholder="***")
        assert "abc123" not in redacted
        assert "***" in redacted

    def test_clean_text_unchanged(self):
        text = "Nothing sensitive here at all."
        assert redact_secrets(text) == text

    def test_multiple_patterns_all_redacted(self):
        text = (
            "api_key='xk_test_ABCDEFGHIJKLMNOPQRST1234'\n"
            "AKIAIOSFODNN7EXAMPLE\n"
            "password = 'MyPasswordHere1234567890'"
        )
        redacted = redact_secrets(text)
        assert "xk_test" not in redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "MyPasswordHere" not in redacted


class TestGuardResult:
    def test_passed_is_ok(self):
        r = GuardResult.passed()
        assert r.ok
        assert str(r) == "PASS"

    def test_flagged_not_ok(self):
        r = GuardResult.flagged("suspicious")
        assert not r.ok
        assert r.action == GuardAction.FLAG
        assert "suspicious" in str(r)

    def test_blocked_not_ok(self):
        r = GuardResult.blocked("secret found")
        assert not r.ok
        assert r.action == GuardAction.BLOCK
