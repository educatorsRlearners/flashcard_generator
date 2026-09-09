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
        for url in self.urls.all():
            counts[url.status] = counts.get(url.status, 0) + 1
        return counts

    @property
    def url_count(self):
        return self.urls.count()

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

    url = models.URLField(max_length=2000, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE, related_name="urls"
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    failure_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.url
