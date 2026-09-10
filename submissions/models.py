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

    url = models.URLField(max_length=2000, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE, related_name="urls"
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
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
