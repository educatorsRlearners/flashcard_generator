from django.contrib import admin

from .models import Batch, Card, SubmittedURL


class SubmittedURLInline(admin.TabularInline):
    model = SubmittedURL
    fields = ("url", "status", "failure_kind", "failure_reason")
    readonly_fields = ("url",)
    extra = 0


class CardInline(admin.TabularInline):
    model = Card
    fields = ("note_type", "source_term", "front", "back", "created_at")
    readonly_fields = ("created_at",)
    extra = 0
    show_change_link = True


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "url_count", "overall_status")
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
        "created_at",
    )
    # issue #9 review state
    list_filter = ("note_type", "batch", "dedup_status", "review_status")
    search_fields = ("source_term", "front", "back", "submitted_url__url")
    readonly_fields = ("created_at", "similarity_score", "embedding")
    list_select_related = ("submitted_url", "batch", "duplicate_of")
    raw_id_fields = ("duplicate_of",)
