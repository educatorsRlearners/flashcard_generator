"""Smoke-test the currently configured LLM provider (issue #86, per
``_docs/llm_portability.md`` §7).

Sends a trivial prompt to whichever provider ``LLM_PROVIDER`` currently
selects and reports success/failure with a clear one-line message, so
switching providers in ``.env`` can be verified in one command before a
real generation batch. Contains no provider-specific branching: it only
calls ``submissions.llm.get_provider()`` / ``submissions.llm.generate()``,
so it works unchanged for any provider registered in
``submissions.llm._PROVIDERS``.
"""

from django.core.management.base import BaseCommand, CommandError

from submissions import llm


class Command(BaseCommand):
    help = (
        "Send a trivial prompt to the currently configured LLM provider "
        "and report success/failure. Exits 0 on success, 1 on any "
        "LLMError (e.g. misconfigured LLM_PROVIDER, missing API key, "
        "rate limit, or bad response)."
    )

    def handle(self, *args, **options):
        try:
            provider = llm.get_provider()
            self.stdout.write(
                f"Provider: {provider.name} (model: {provider.model})"
            )
            llm.generate(system="", prompt="reply with the word OK", max_tokens=16)
        except llm.LLMError as exc:
            message = f"FAILED: {type(exc).__name__}: {exc}"
            self.stdout.write(message)
            raise CommandError(message)

        self.stdout.write("OK - received a non-empty reply")
