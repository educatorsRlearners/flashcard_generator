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
    list_display = ("url", "batch", "status", "created_at")
    list_filter = ("status",)
    list_editable = ("status",)
    fields = ("url", "batch", "status", "failure_reason", "created_at")
    readonly_fields = ("created_at",)
    search_fields = ("url",)
