from django.contrib import admin

from .models import Batch, SubmittedURL


class SubmittedURLInline(admin.TabularInline):
    model = SubmittedURL
    fields = ("url", "status", "failure_reason")
    readonly_fields = ("url",)
    extra = 0


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
        "extraction_method",
        "extracted_title",
        "extracted_at",
        "created_at",
    )
    list_filter = ("status", "extraction_method")
    list_editable = ("status",)
    fields = (
        "url",
        "batch",
        "status",
        "failure_reason",
        "extraction_method",
        "extracted_title",
        "extracted_at",
        "extracted_text",
        "created_at",
    )
    readonly_fields = ("created_at", "extracted_text", "extracted_at")
    search_fields = ("url",)
