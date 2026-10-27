"""The shared repair budget (addendum section 7): persisted, atomic, never reset."""

import pytest

from lib import artifacts, jobs, repair_budget
from tests.fakes import FakeS3, FakeTable


@pytest.fixture
def v2_job(monkeypatch):
    table, s3 = FakeTable(), FakeS3()
    monkeypatch.setattr(jobs, "_tbl", lambda: table)
    monkeypatch.setattr(artifacts, "_client", lambda: s3)
    jobs.create_job("job-b", "owner", "hash", "k", {"width": 1080, "height": 1080},
                    pipeline_version="layered-v2", template_flexibility=20)
    return "job-b"


def test_the_defaults_are_one_template_repair_two_css_rounds_no_photo(v2_job):
    job = jobs.get_job_unscoped(v2_job)
    assert repair_budget.limits(job) == {"templateImage": 1, "domCss": 2, "photoImage": 0}
    assert repair_budget.snapshot(job)["remaining"] == {
        "templateImage": 1, "domCss": 2, "photoImage": 0}


def test_each_kind_can_be_spent_exactly_up_to_its_limit(v2_job):
    assert repair_budget.consume(v2_job, "templateImage") == 1
    assert repair_budget.consume(v2_job, "templateImage") is None      # second is refused
    assert repair_budget.consume(v2_job, "domCss") == 1
    assert repair_budget.consume(v2_job, "domCss") == 2
    assert repair_budget.consume(v2_job, "domCss") is None

    snapshot = repair_budget.snapshot(jobs.get_job_unscoped(v2_job))
    assert snapshot["used"] == {"templateImage": 1, "domCss": 2, "photoImage": 0}
    assert snapshot["remaining"] == {"templateImage": 0, "domCss": 0, "photoImage": 0}


def test_a_template_repair_cannot_buy_a_photograph_rebuild(v2_job):
    """photoImage defaults to zero, so nothing rebuilds the photograph as a side effect."""
    repair_budget.consume(v2_job, "templateImage")
    assert repair_budget.consume(v2_job, "photoImage") is None


def test_a_photograph_repair_needs_a_documented_reason_and_is_capped_at_one(v2_job):
    with pytest.raises(ValueError):
        repair_budget.grant_photo_repair(v2_job, "  ")
    assert repair_budget.consume(v2_job, "photoImage") is None          # still none

    assert repair_budget.grant_photo_repair(
        v2_job, "Residual baked headline visible in the cleaned photograph.",
        evidence={"findingCode": "BAKED_TEXT_IN_PHOTO"}) is True
    job = jobs.get_job_unscoped(v2_job)
    assert job["repairBudget"]["photoGrant"]["reason"].startswith("Residual baked")
    assert repair_budget.consume(v2_job, "photoImage") == 1
    assert repair_budget.consume(v2_job, "photoImage") is None          # exactly one

    # A second grant does not reopen it.
    assert repair_budget.grant_photo_repair(v2_job, "again") is False
    assert repair_budget.consume(v2_job, "photoImage") is None


def test_the_limits_are_the_ones_stored_with_the_job_not_the_current_defaults(
        v2_job, monkeypatch):
    monkeypatch.setitem(repair_budget.DEFAULT_LIMITS, "templateImage", 5)
    # The job was created with 1; a later change to the default does not move its bar.
    stored = jobs.get_job_unscoped(v2_job)["repairBudget"]["limits"]
    assert stored["templateImage"] == 1
    assert repair_budget.limits({"repairBudget": {"limits": stored}})["templateImage"] == 1


def test_an_unknown_kind_is_a_programming_error(v2_job):
    with pytest.raises(ValueError):
        repair_budget.consume(v2_job, "everything")
