"""Tests for LLM call observability (issue #28).

Every call through ``submissions.llm.generate`` records an ``LLMCall`` row
with usage (from the provider payload), latency, estimated cost and
attribution. The provider SDK / HTTP layer is stubbed with ``monkeypatch`` -
no network, no ``ANTHROPIC_API_KEY``.
"""

import types
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, override_settings

from submissions import generation, llm
from submissions.models import Batch, BatchRequest, LLMCall, SubmittedURL

pytestmark = pytest.mark.django_db


# --- fakes (same shape as tests/test_llm.py) ---------------------------


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _response(text="hello", stop_reason="end_turn", *, in_tok=10, out_tok=5):
    return types.SimpleNamespace(
        content=[_text_block(text)],
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class FakeAPIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeMessages:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[idx]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, results):
        self.messages = FakeMessages(results)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "_sleep", lambda seconds: None)


@pytest.fixture
def anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _install_client(monkeypatch, results):
    client = FakeClient(results)
    monkeypatch.setattr(llm, "_new_anthropic_client", lambda api_key: client)
    return client


# --- per-call row ------------------------------------------------------


def test_successful_call_records_row_with_usage_latency_cost(
    monkeypatch, anthropic_key
):
    _install_client(monkeypatch, [_response("hi", in_tok=12, out_tok=7)])

    result = llm.generate(system="s", prompt="p")
    assert result.text == "hi"

    row = LLMCall.objects.get()
    assert row.status == LLMCall.Status.OK
    assert row.error_class == ""
    assert row.model == "claude-sonnet-5"
    # Token counts come from the provider usage payload.
    assert (row.prompt_tokens, row.completion_tokens) == (12, 7)
    assert row.total_tokens == 19
    assert row.latency_ms >= 0
    assert row.estimated_cost_usd == llm.estimate_cost_usd(
        "claude-sonnet-5", 12, 7
    )
    assert row.estimated_cost_usd > Decimal("0")
    assert row.batch is None and row.submitted_url is None


def test_explicit_kwargs_attribute_row(monkeypatch, anthropic_key):
    _install_client(monkeypatch, [_response("hi")])
    batch = Batch.objects.create()
    url = SubmittedURL.objects.create(url="https://example.com/a")

    llm.generate(system="s", prompt="p", batch=batch, submitted_url=url)

    row = LLMCall.objects.get()
    assert row.batch_id == batch.pk
    assert row.submitted_url_id == url.pk


def test_call_context_attribute_row(monkeypatch, anthropic_key):
    _install_client(monkeypatch, [_response("hi")])
    batch = Batch.objects.create()
    url = SubmittedURL.objects.create(url="https://example.com/a")

    with llm.call_context(batch=batch, submitted_url=url):
        llm.generate(system="s", prompt="p")

    row = LLMCall.objects.get()
    assert row.batch_id == batch.pk
    assert row.submitted_url_id == url.pk


def test_generation_run_attributes_call_to_batch_and_url(
    monkeypatch, anthropic_key
):
    """End-to-end: generate_for -> real client (stubbed transport) records
    an attributed row. Dedup/images are best-effort side steps, stubbed out
    so this test needs no model weights or network."""
    _install_client(
        monkeypatch,
        [_response('{"cards": [{"note_type": "basic", "front": "Q?", '
                    '"back": "A.", "source_term": "T", "topic": ""}]}')],
    )
    monkeypatch.setattr(generation.dedup, "dedup_cards", lambda cards: None)
    monkeypatch.setattr(
        generation.images, "attach_images", lambda *a, **k: None
    )
    batch = Batch.objects.create()
    url = SubmittedURL.objects.create(
        url="https://example.com/bio",
        status=SubmittedURL.Status.OK,
        extraction_method=SubmittedURL.ExtractionMethod.STATIC,
        extracted_text=("Photosynthesis converts light energy. " * 40),
        extracted_title="Bio",
        batch=batch,
    )
    BatchRequest.objects.create(batch=batch, submitted_url=url)

    result = generation.generate_for(url)

    assert result.outcome == "created"
    row = LLMCall.objects.get()
    assert row.status == LLMCall.Status.OK
    assert row.batch_id == batch.pk
    assert row.submitted_url_id == url.pk
    assert (row.prompt_tokens, row.completion_tokens) == (10, 5)


# --- failed calls ------------------------------------------------------


def test_failed_call_records_failed_row_with_error_class(
    monkeypatch, anthropic_key
):
    _install_client(monkeypatch, [FakeAPIError(503)])

    with pytest.raises(llm.LLMTransientError):
        llm.generate(system="s", prompt="p")

    row = LLMCall.objects.get()
    assert row.status == LLMCall.Status.FAILED
    assert row.error_class == "LLMTransientError"
    assert (row.prompt_tokens, row.completion_tokens) == (0, 0)
    assert row.estimated_cost_usd == Decimal("0")


def test_config_error_records_failed_row(monkeypatch):
    with override_settings(LLM_PROVIDER="does-not-exist"):
        with pytest.raises(llm.LLMConfigError):
            llm.generate(system="s", prompt="p")

    row = LLMCall.objects.get()
    assert row.status == LLMCall.Status.FAILED
    assert row.error_class == "LLMConfigError"


# --- cost helper -------------------------------------------------------


def test_estimate_cost_usd_uses_per_model_prices_with_fallback():
    assert llm.estimate_cost_usd("claude-sonnet-5", 1_000_000, 0) == Decimal(
        "3.000000"
    )
    assert llm.estimate_cost_usd("claude-haiku-1", 0, 1_000_000) == Decimal(
        "4.000000"
    )
    # Unknown model -> DEFAULT_* fallback rates.
    assert llm.estimate_cost_usd("mystery-model", 1_000_000, 1_000_000) == Decimal(
        str(
            llm.DEFAULT_INPUT_USD_PER_MTOK + llm.DEFAULT_OUTPUT_USD_PER_MTOK
        )
    ).quantize(Decimal("0.000001"))


# --- admin + management command smoke ----------------------------------


def test_llmcall_visible_in_admin():
    LLMCall.objects.create(
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=42,
        estimated_cost_usd=Decimal("0.000105"),
        status=LLMCall.Status.OK,
    )
    User.objects.create_superuser("admin", "a@example.com", "pw")
    client = Client()
    client.force_login(User.objects.get(username="admin"))

    changelist = client.get("/admin/submissions/llmcall/")
    assert changelist.status_code == 200
    assert b"claude-sonnet-5" in changelist.content


def test_llm_usage_command_lists_rows_and_totals(monkeypatch, anthropic_key):
    _install_client(
        monkeypatch, [_response("hi", in_tok=10, out_tok=5), FakeAPIError(500)]
    )
    llm.generate(system="s", prompt="p")
    with pytest.raises(llm.LLMTransientError):
        llm.generate(system="s", prompt="p")

    out = StringIO()
    call_command("llm_usage", stdout=out)
    text = out.getvalue()
    assert "in=10 out=5" in text
    assert "failed" in text
    assert "2 calls: 15 tokens" in text

    out = StringIO()
    call_command("llm_usage", "--status", "failed", stdout=out)
    assert "1 calls" in out.getvalue()
