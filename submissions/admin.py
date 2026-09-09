from django.contrib import admin

from .models import SubmittedURL


@admin.register(SubmittedURL)
class SubmittedURLAdmin(admin.ModelAdmin):
    list_display = ("url", "created_at")
    search_fields = ("url",)
