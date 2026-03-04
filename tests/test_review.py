"""Tests for the Review model — verdicts, helpers, identity."""

from uuid import uuid4

from schwarma.review import Review, ReviewType, ReviewVerdict


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_review(**kw) -> Review:
    defaults = dict(
        solution_id=uuid4(),
        reviewer_id=uuid4(),
        review_type=ReviewType.CORRECTNESS,
        verdict=ReviewVerdict.APPROVE,
    )
    defaults.update(kw)
    return Review(**defaults)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestReviewCreation:
    def test_defaults(self):
        r = _make_review()
        assert r.verdict == ReviewVerdict.APPROVE
        assert r.confidence == 1.0
        assert r.body == ""
        assert r.review_type == ReviewType.CORRECTNESS

    def test_custom_fields(self):
        r = _make_review(
            review_type=ReviewType.GOOD_FAITH,
            verdict=ReviewVerdict.REJECT,
            confidence=0.7,
            body="Looks suspicious",
        )
        assert r.review_type == ReviewType.GOOD_FAITH
        assert r.verdict == ReviewVerdict.REJECT
        assert r.confidence == 0.7
        assert r.body == "Looks suspicious"


class TestVerdictHelpers:
    def test_approve_is_positive(self):
        r = _make_review(verdict=ReviewVerdict.APPROVE)
        assert r.is_positive
        assert not r.is_negative

    def test_reject_is_negative(self):
        r = _make_review(verdict=ReviewVerdict.REJECT)
        assert r.is_negative
        assert not r.is_positive

    def test_request_changes_is_negative(self):
        r = _make_review(verdict=ReviewVerdict.REQUEST_CHANGES)
        assert r.is_negative
        assert not r.is_positive

    def test_abstain_is_neither(self):
        r = _make_review(verdict=ReviewVerdict.ABSTAIN)
        assert not r.is_positive
        assert not r.is_negative


class TestIdentity:
    def test_hashable(self):
        r = _make_review()
        assert hash(r) == hash(r.id)

    def test_equality_by_id(self):
        r1 = _make_review()
        r2 = _make_review()
        assert r1 != r2

    def test_str(self):
        r = _make_review()
        text = str(r)
        assert "APPROVE" in text
        assert "CORRECTNESS" in text


class TestReviewTypes:
    def test_all_types_exist(self):
        assert ReviewType.CORRECTNESS
        assert ReviewType.GOOD_FAITH
        assert ReviewType.PROOFREADING
        assert ReviewType.QUALITY
