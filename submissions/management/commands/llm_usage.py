"""Show recorded LLM call usage, latency and cost (issue #28).

Read-only view over the ``LLMCall`` rows written by ``submissions.llm``:
one line per call plus totals. The same rows are visible in the Django
admin (``LLMCall``).
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from submissions.models import LLMCall


class Command(BaseCommand):
    help = (
        "List recorded LLM calls (model, tokens, latency ms, estimated "
        "USD cost, status) with totals. Filter with --batch, --url, "
        "or --status; limit rows with --limit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch", type=int, default=None, help="Only calls for this batch id."
        )
        parser.add_argument(
            "--url",
            default=None,
            help="Only calls for the SubmittedURL with this URL.",
        )
        parser.add_argument(
            "--status",
            choices=("ok", "failed"),
            default=None,
            help="Only calls with this status.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Max rows to list (default 50). Totals always cover the full filter.",
        )

    def handle(self, *args, **options):
        queryset = LLMCall.objects.all().order_by("-created_at", "-id")
        if options["batch"] is not None:
            queryset = queryset.filter(batch_id=options["batch"])
        if options["url"] is not None:
            queryset = queryset.filter(submitted_url__url=options["url"])
        if options["status"] is not None:
            queryset = queryset.filter(status=options["status"])

        calls = list(queryset[: options["limit"]])
        total_tokens = 0
        total_cost = Decimal("0")
        failed = 0
        for call in queryset.only(
            "prompt_tokens", "completion_tokens", "estimated_cost_usd", "status"
        ).iterator():
            total_tokens += (call.prompt_tokens or 0) + (call.completion_tokens or 0)
            total_cost += call.estimated_cost_usd or Decimal("0")
            if call.status == LLMCall.Status.FAILED:
                failed += 1

        if not calls:
            self.stdout.write("no LLM calls recorded")
        for call in reversed(calls):  # oldest-first for a stable reading order
            self.stdout.write(
                f"{call.created_at:%Y-%m-%d %H:%M} {call.model} "
                f"in={call.prompt_tokens} out={call.completion_tokens} "
                f"{call.latency_ms}ms ${call.estimated_cost_usd} "
                f"{call.status}"
                + (f" {call.error_class}" if call.error_class else "")
                + (
                    f" batch={call.batch_id}"
                    if call.batch_id is not None
                    else ""
                )
            )
        self.stdout.write(
            f"{queryset.count()} calls: {total_tokens} tokens, "
            f"${total_cost} estimated, {failed} failed"
        )
