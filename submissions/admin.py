from django.contrib import admin

from .models import Batch, Card, Feedback, LLMCall, SubmittedURL


class SubmittedURLInline(admin.TabularInline):
    model = SubmittedURL
    fields = ("url", "status", "failure_kind", "failure_reason")
    readonly_fields = ("url",)
    extra = 0


class CardInline(admin.TabularInline):
    model = Card
    fields = ("note_type", "source_term", "front", "back", "image_source", "created_at")
    readonly_fields = ("created_at", "image_source")
    extra = 0
    show_change_link = True


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "deck_name", "url_count", "overall_status")
    inlines = (SubmittedURLInline,)


@admin.register(SubmittedURL)
class SubmittedURLAdmin(admin.ModelAdmin):
    list_display = (
        "url",
        "batch",
        "status",
        "failure_kind",
        "extraction_method",
        "extracted_title",
        "extracted_at",
        "generation_status",
        "created_at",
    )
    list_filter = ("status", "failure_kind", "extraction_method", "generation_status")
    list_editable = ("status",)
    fields = (
        "url",
        "batch",
        "status",
        "failure_kind",
        "failure_reason",
        "extraction_method",
        "extracted_title",
        "extracted_at",
        "extracted_text",
        "generation_status",
        "generation_error",
        "created_at",
    )
    readonly_fields = (
        "created_at",
        "extracted_text",
        "extracted_at",
        "failure_kind",
        "failure_reason",
        "generation_status",
        "generation_error",
    )
    search_fields = ("url",)
    inlines = (CardInline,)


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = (
        "note_type",
        "source_term",
        "submitted_url",
        "batch",
        "dedup_status",
        "duplicate_of",
        "similarity_score",
        "image_source",
        "created_at",
    )
    list_filter = ("note_type", "batch", "dedup_status", "review_status", "image_source")
    search_fields = ("source_term", "front", "back", "submitted_url__url")
    readonly_fields = ("created_at", "similarity_score", "embedding", "image_source")
    list_select_related = ("submitted_url", "batch", "duplicate_of")
    raw_id_fields = ("duplicate_of",)


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin):
    """Per-call LLM usage / cost history (issue #28). Read-only: rows are
    written by the client and are never edited here."""

    list_display = (
        "created_at",
        "model",
        "status",
        "error_class",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "estimated_cost_usd",
        "batch",
        "submitted_url",
    )
    list_filter = ("status", "model", "error_class")
    search_fields = ("model", "error_class", "submitted_url__url")
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "estimated_cost_usd",
        "status",
        "error_class",
        "batch",
        "submitted_url",
    )
    list_select_related = ("batch", "submitted_url")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    """Durable review-decision history (issue #10). Read-only: rows are
    written by the review grid and are never edited here."""

    list_display = ("decision", "note_type", "source_url", "front", "created_at")
    list_filter = ("decision", "note_type")
    search_fields = ("front", "back", "reason", "source_url")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
