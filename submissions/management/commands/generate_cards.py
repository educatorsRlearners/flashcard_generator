"""Generate flashcards for extracted SubmittedURL rows (issue #6).

Thin CLI wrapper over :mod:`submissions.generation`: it selects rows, calls
``generate_for`` per row, prints one line per URL, and maps the typed LLM
errors from :mod:`submissions.llm` to behaviour:

* ``LLMAuthError`` / ``LLMConfigError`` (provider config missing / broken) ->
  stop now with a non-zero exit (it would fail for every URL).
* ``LLMRateLimitError`` / ``LLMTransientError`` -> mark that URL
  failed-to-generate, keep going, exit 0.
* ``LLMBadResponseError`` (refusal / truncation / malformed) -> skip that URL
  with the reason, keep going, exit 0.
"""

from django.core.management.base import BaseCommand, CommandError

from submissions import llm
from submissions.generation import generate_for, mark_generation_failed
from submissions.models import SubmittedURL


class Command(BaseCommand):
    help = (
        "Generate Anki-style flashcards from the extracted text of "
        "SubmittedURL rows.\n\n"
        "With no selector, every row that has been extracted "
        "(extraction_method != 'none'), has status 'ok', and has no cards "
        "yet is processed. --force deletes a URL's existing cards and "
        "regenerates."
    )

    def add_arguments(self, parser):
        selector = parser.add_mutually_exclusive_group()
        selector.add_argument("--url", help="Generate for the single row with this URL.")
        selector.add_argument(
            "--id", type=int, help="Generate for the single row with this primary key."
        )
        selector.add_argument(
            "--batch",
            type=int,
            help="Generate for every extracted URL in this batch.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete and regenerate cards for URLs that already have them.",
        )

    def handle(self, *args, **options):
        queryset = self._select(options)
        force = options["force"]

        for submitted_url in queryset:
            try:
                result = generate_for(submitted_url, force=force)
            except (llm.LLMAuthError, llm.LLMConfigError) as exc:
                raise CommandError(
                    f"aborting: LLM client is unusable ({exc}). "
                    "Fix the provider configuration and re-run."
                )
            except (llm.LLMRateLimitError, llm.LLMTransientError) as exc:
                reason = str(exc) or "LLM temporarily unavailable"
                mark_generation_failed(submitted_url, reason)
                self.stdout.write(f"{submitted_url.url} failed: {reason}")
                continue
            except llm.LLMBadResponseError as exc:
                reason = str(exc) or "unusable LLM response"
                mark_generation_failed(submitted_url, reason)
                self.stdout.write(f"{submitted_url.url} skipped: {reason}")
                continue

            self.stdout.write(result.summary_line(submitted_url.url))

    def _select(self, options):
        if options["url"]:
            try:
                return [SubmittedURL.objects.get(url=options["url"])]
            except SubmittedURL.DoesNotExist:
                raise CommandError(f"No SubmittedURL with url {options['url']!r}")
        if options["id"]:
            try:
                return [SubmittedURL.objects.get(pk=options["id"])]
            except SubmittedURL.DoesNotExist:
                raise CommandError(f"No SubmittedURL with id {options['id']}")
        if options["batch"]:
            return list(
                SubmittedURL.objects.filter(
                    requests__batch_id=options["batch"]
                )
                .exclude(extraction_method=SubmittedURL.ExtractionMethod.NONE)
                .distinct()
                .order_by("pk")
            )
        return list(
            SubmittedURL.objects.filter(
                status=SubmittedURL.Status.OK,
                cards__isnull=True,
            )
            .exclude(extraction_method=SubmittedURL.ExtractionMethod.NONE)
            .order_by("pk")
        )
