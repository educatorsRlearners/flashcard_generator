"""Extract main content for SubmittedURL rows.

Thin CLI wrapper over :mod:`submissions.extraction`: selects rows, calls
``extract`` per row, prints one line per URL, and always exits 0.
"""

from django.core.management.base import BaseCommand, CommandError

from submissions.extraction import extract
from submissions.models import SubmittedURL


class Command(BaseCommand):
    help = "Fetch and extract main text content for SubmittedURL rows."

    def add_arguments(self, parser):
        selector = parser.add_mutually_exclusive_group()
        selector.add_argument("--url", help="Extract the single row with this URL.")
        selector.add_argument(
            "--id", type=int, help="Extract the single row with this primary key."
        )
        selector.add_argument(
            "--batch", type=int, help="Extract every URL in this batch."
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-extract rows whose extraction_method is not 'none'.",
        )

    def handle(self, *args, **options):
        queryset = self._select(options)
        force = options["force"]
        single = options["url"] or options["id"]

        for submitted_url in queryset:
            if (
                not force
                and not single
                and submitted_url.extraction_method
                != SubmittedURL.ExtractionMethod.NONE
            ):
                self.stdout.write(
                    f"{submitted_url.url} skipped (already extracted; use --force)"
                )
                continue

            try:
                result = extract(submitted_url, force=force)
            except Exception as exc:  # never raise on a single bad URL
                submitted_url.status = SubmittedURL.Status.FAILED
                submitted_url.failure_reason = f"unexpected error: {exc}"
                submitted_url.save()
                self.stdout.write(f"{submitted_url.url} failed {exc}")
                continue

            if result.outcome == "ok":
                self.stdout.write(
                    f"{submitted_url.url} {result.method} {result.char_count} chars"
                )
            else:
                self.stdout.write(
                    f"{submitted_url.url} {result.method} {result.reason}"
                )

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
                SubmittedURL.objects.filter(batch_id=options["batch"]).order_by("pk")
            )
        return list(
            SubmittedURL.objects.filter(
                extraction_method=SubmittedURL.ExtractionMethod.NONE
            ).order_by("pk")
        )
