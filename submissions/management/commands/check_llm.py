"""Smoke-test the currently configured (or overridden) LLM provider (issue
#86, extended by #99 with an optional ``--provider`` override), per
``_docs/llm_portability.md`` §7.

Sends a trivial prompt to whichever provider ``LLM_PROVIDER`` currently
selects - or, when ``--provider <name>`` is passed, to that named provider
instead - and reports success/failure with a clear one-line message, so
switching providers can be verified in one command before a real generation
batch, without editing ``.env`` first. Contains no provider-specific
branching: it only calls ``submissions.llm.get_provider(name=...)`` and the
returned ``Provider`` instance's own ``.generate(...)``, so it works
unchanged for any provider registered in ``submissions.llm._PROVIDERS``.

Both the identification line and the actual round-trip call go through the
same ``Provider`` instance ``get_provider(name=...)`` returns. The
module-level ``submissions.llm.generate()`` is deliberately not used here:
it re-resolves ``get_provider()`` internally with no name argument, so it
would always exercise the env-configured default provider even when
``--provider`` selected a different one.
"""

from django.core.management.base import BaseCommand, CommandError

from submissions import llm


class Command(BaseCommand):
    help = (
        "Send a trivial prompt to the currently configured (or, with "
        "--provider, an overridden) LLM provider and report "
        "success/failure. Exits 0 on success, 1 on any LLMError (e.g. "
        "misconfigured provider, missing API key, rate limit, or bad "
        "response)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            default=None,
            help=(
                "Name of a provider registered in submissions.llm._PROVIDERS "
                "to test instead of the env-configured LLM_PROVIDER. Omit to "
                "test the default provider (today's behavior)."
            ),
        )

    def handle(self, *args, **options):
        try:
            provider = llm.get_provider(name=options["provider"])
            self.stdout.write(
                f"Provider: {provider.name} (model: {provider.model})"
            )
            provider.generate(system="", prompt="reply with the word OK", max_tokens=16)
        except llm.LLMError as exc:
            message = f"FAILED: {type(exc).__name__}: {exc}"
            self.stdout.write(message)
            raise CommandError(message)

        self.stdout.write("OK - received a non-empty reply")
