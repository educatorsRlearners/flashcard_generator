"""Tests for LLM cost/failure-rate alerting (issue #90).

``submissions.tasks.check_llm_alerts_task`` is a ``db_periodic_task`` that
reads recent ``LLMCall`` rows (issue #28) and logs a WARNING when total
estimated cost or failure rate over a rolling window crosses a configured
threshold. Huey immediate mode (per AGENTS.md / ``tests/conftest.py``) lets
the task be invoked directly like a plain function; ``created_at`` is
``auto_now_add`` so rows are created normally and then backdated with a
direct ``.update()`` (never by round-tripping through ``save()``, which
would re-stamp it) - same pattern as ``tests/test_llm_usage_dashboard.py``.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from submissions import tasks
from submissions.models import LLMCall

pytestmark = pytest.mark.django_db


def _make_call(*, cost="0", status=LLMCall.Status.OK, age=timedelta(minutes=1)):
    call = LLMCall.objects.create(
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=100,
        estimated_cost_usd=Decimal(str(cost)),
        status=status,
    )
    LLMCall.objects.filter(pk=call.pk).update(created_at=timezone.now() - age)
    return call


@pytest.fixture(autouse=True)
def _reset_alert_state():
    """Each dimension's breach state is process-global; isolate tests."""
    tasks._alert_breached["cost"] = False
    tasks._alert_breached["failure_rate"] = False
    yield
    tasks._alert_breached["cost"] = False
    tasks._alert_breached["failure_rate"] = False


# --- Cost check ------------------------------------------------------------


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="10.00",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_cost_threshold_crossed_logs_warning(caplog):
    _make_call(cost="6.00")
    _make_call(cost="7.00")  # total 13.00 > 10.00

    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].message
    assert "LLM" in message
    assert "cost" in message
    assert "13.00" in message
    assert "10.00" in message
    assert "60" in message


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="10.00",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_cost_threshold_not_crossed_logs_nothing(caplog):
    _make_call(cost="1.00")
    _make_call(cost="2.00")  # total 3.00 < 10.00

    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    assert caplog.records == []


# --- Failure-rate check ------------------------------------------------


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="50",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_failure_rate_threshold_crossed_logs_warning(caplog):
    _make_call(status=LLMCall.Status.FAILED)
    _make_call(status=LLMCall.Status.FAILED)
    _make_call(status=LLMCall.Status.OK)  # 2/3 = 66.7% > 50%

    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].message
    assert "LLM" in message
    assert "failure" in message.lower()
    assert "66.7" in message
    assert "2/3" in message
    assert "50" in message
    assert "60" in message


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="50",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_failure_rate_threshold_not_crossed_logs_nothing(caplog):
    _make_call(status=LLMCall.Status.FAILED)
    _make_call(status=LLMCall.Status.OK)
    _make_call(status=LLMCall.Status.OK)  # 1/3 = 33.3% < 50%

    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    assert caplog.records == []


# --- Zero calls in window ----------------------------------------------


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="10.00",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="50",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_zero_calls_in_window_skips_failure_rate_no_crash(caplog):
    # No LLMCall rows at all.
    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    # Cost check still "runs" (sum is 0, never exceeds a positive
    # threshold) and the failure-rate check is skipped outright - no
    # division by zero, no alert, no log line at all.
    assert caplog.records == []


# --- No thresholds configured -------------------------------------------


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_no_thresholds_configured_is_noop(caplog, django_assert_num_queries):
    _make_call(cost="999.00", status=LLMCall.Status.FAILED)

    with caplog.at_level("DEBUG", logger="submissions.tasks"):
        with django_assert_num_queries(0):
            tasks.check_llm_alerts_task.call_local()

    assert caplog.records == []


# --- Re-alert suppression / recovery ------------------------------------


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="10.00",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="",
    LLM_ALERT_WINDOW_MINUTES="60",
)
def test_consecutive_breaches_alert_once_then_recover_then_realert(caplog):
    _make_call(cost="20.00")  # breached: 20 > 10

    with caplog.at_level("INFO", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()  # 1st breached check: WARNING
        tasks.check_llm_alerts_task.call_local()  # still breached: no repeat

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1

    caplog.clear()

    # Drop below threshold: recovery.
    LLMCall.objects.all().delete()
    with caplog.at_level("INFO", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1
    assert "recover" in infos[0].message.lower()
    assert caplog.records == infos  # no WARNING logged on recovery

    caplog.clear()

    # Re-breach: alerts again.
    _make_call(cost="20.00")
    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


# --- Malformed settings values -------------------------------------------


@override_settings(
    LLM_ALERT_COST_USD_THRESHOLD="not-a-number",
    LLM_ALERT_FAILURE_RATE_THRESHOLD="-5",
    LLM_ALERT_WINDOW_MINUTES="banana",
)
def test_malformed_settings_disable_checks_without_raising(caplog):
    _make_call(cost="999.00", status=LLMCall.Status.FAILED)

    with caplog.at_level("WARNING", logger="submissions.tasks"):
        tasks.check_llm_alerts_task.call_local()  # must not raise

    assert caplog.records == []


def test_window_respected_calls_outside_window_ignored(caplog):
    _make_call(cost="20.00", age=timedelta(minutes=90))  # outside 60 min window

    with override_settings(
        LLM_ALERT_COST_USD_THRESHOLD="10.00",
        LLM_ALERT_FAILURE_RATE_THRESHOLD="",
        LLM_ALERT_WINDOW_MINUTES="60",
    ):
        with caplog.at_level("WARNING", logger="submissions.tasks"):
            tasks.check_llm_alerts_task.call_local()

    assert caplog.records == []
