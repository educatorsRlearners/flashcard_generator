"""Mint, rotate, or show the browser-extension shared-secret token (#33).

The token authenticates the browser extension's requests to this backend
(see #35, #37/#38). Exactly one of ``--mint``, ``--rotate``, or ``--show``
must be given.
"""

from django.core.management.base import BaseCommand, CommandError

from submissions.extension_auth import mint_token, read_token


class Command(BaseCommand):
    help = (
        "Mint, rotate, or show the local shared-secret token used to "
        "authenticate browser-extension requests to this backend."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--mint",
            action="store_true",
            help="Mint a token if none exists yet. Fails if one already does.",
        )
        group.add_argument(
            "--rotate",
            action="store_true",
            help="Mint a new token, overwriting any existing one.",
        )
        group.add_argument(
            "--show",
            action="store_true",
            help="Print the current token. Fails if none has been minted.",
        )

    def handle(self, *args, **options):
        if options["mint"]:
            try:
                token = mint_token(force=False)
            except FileExistsError:
                raise CommandError(
                    "An extension token already exists; use --rotate to "
                    "replace it."
                )
            self.stdout.write(token)
        elif options["rotate"]:
            token = mint_token(force=True)
            self.stdout.write(token)
        else:  # --show
            token = read_token()
            if token is None:
                raise CommandError(
                    "No extension token exists yet; run --mint first."
                )
            self.stdout.write(token)
