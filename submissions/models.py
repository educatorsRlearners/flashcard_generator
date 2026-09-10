from django.db import models


class Batch(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Batch {self.pk} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def status_counts(self):
        counts = {choice: 0 for choice in SubmittedURL.Status.values}
        for request in self.requests.select_related("submitted_url"):
            status = request.submitted_url.status
            counts[status] = counts.get(status, 0) + 1
        return counts

    @property
    def failure_kind_counts(self):
        counts = {}
        for request in self.requests.select_related("submitted_url"):
            submitted_url = request.submitted_url
            if submitted_url.status != SubmittedURL.Status.FAILED:
                continue
            kind = submitted_url.failure_kind or SubmittedURL.FailureKind.UNKNOWN
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @property
    def failure_kind_summary(self):
        counts = self.failure_kind_counts
        if not counts:
            return ""
        total = sum(counts.values())
        parts = ", ".join(
            f"{n} {SubmittedURL.short_failure_label(kind)}"
            for kind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        return f"{total} failed: {parts}"

    @property
    def url_count(self):
        return self.requests.count()

    @property
    def overall_status(self):
        counts = self.status_counts
        if counts.get(SubmittedURL.Status.PENDING, 0) > 0:
            return "pending"
        return "complete"

    @property
    def status_summary(self):
        counts = self.status_counts
        return "{pending} pending, {ok} ok, {failed} failed".format(
            pending=counts.get(SubmittedURL.Status.PENDING, 0),
            ok=counts.get(SubmittedURL.Status.OK, 0),
            failed=counts.get(SubmittedURL.Status.FAILED, 0),
        )


class SubmittedURL(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        OK = "ok", "OK"
        FAILED = "failed", "Failed"

    class ExtractionMethod(models.TextChoices):
        NONE = "none", "None"
        STATIC = "static", "Static"
        BROWSER = "browser", "Browser"

    class FailureKind(models.TextChoices):
        """Machine-readable category for a failed extraction.

        The blank default ("") means "not failed". #17 will add
        ``blocked_by_robots`` and ``retries_exhausted`` here.
        """

        DNS = "dns", "DNS - host not found"
        CONNECTION = "connection", "Connection failed"
        HTTP_CLIENT = "http_client", "HTTP client error (4xx)"
        BLOCKED = "blocked", "Blocked / rate-limited (401/403/429)"
        TIMEOUT = "timeout", "Timed out"
        TOO_LARGE = "too_large", "Response too large"
        UNSUPPORTED_TYPE = "unsupported_type", "Unsupported content type"
        NO_CONTENT = "no_content", "No extractable content"
        UNKNOWN = "unknown", "Unknown / uncategorised"

    url = models.URLField(max_length=2000, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    batch = models.ForeignKey(
        Batch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="urls",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    failure_kind = models.CharField(
        max_length=20, choices=FailureKind.choices, blank=True, default=""
    )
    failure_reason = models.TextField(blank=True, default="")
    extracted_text = models.TextField(blank=True, default="")
    extracted_title = models.CharField(max_length=500, blank=True, default="")
    extraction_method = models.CharField(
        max_length=16,
        choices=ExtractionMethod.choices,
        default=ExtractionMethod.NONE,
    )
    extracted_at = models.DateTimeField(null=True, blank=True)

    #: Short human-readable forms of ``FailureKind`` for use in running
    #: prose (e.g. the batch by-kind breakdown). The full ``.label`` values
    #: are used verbatim for per-row display via ``get_failure_kind_display``.
    SHORT_FAILURE_LABELS = {
        FailureKind.DNS: "host not found",
        FailureKind.CONNECTION: "connection failed",
        FailureKind.HTTP_CLIENT: "client error (4xx)",
        FailureKind.BLOCKED: "blocked / rate-limited",
        FailureKind.TIMEOUT: "timeout",
        FailureKind.TOO_LARGE: "response too large",
        FailureKind.UNSUPPORTED_TYPE: "unsupported content type",
        FailureKind.NO_CONTENT: "no extractable content",
        FailureKind.UNKNOWN: "unknown",
    }

    @classmethod
    def short_failure_label(cls, kind):
        """Human-readable short label for a ``FailureKind`` value."""
        if not kind:
            kind = cls.FailureKind.UNKNOWN
        try:
            return cls.SHORT_FAILURE_LABELS.get(
                cls.FailureKind(kind), cls.FailureKind(kind).label
            )
        except ValueError:
            return str(kind)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.url


class BatchRequest(models.Model):
    """Records that a batch requested a given URL.

    Acts as a through-model between Batch and SubmittedURL so a URL that is
    re-submitted in a later batch is tracked for every batch that asked for it.
    """

    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE, related_name="requests"
    )
    submitted_url = models.ForeignKey(
        SubmittedURL, on_delete=models.CASCADE, related_name="requests"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("batch", "submitted_url")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Batch {self.batch_id} -> {self.submitted_url_id}"
