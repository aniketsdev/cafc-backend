from django.contrib import admin
from .models import Provider


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "specialty", "phone_number", "fax_number", "lead", "created_at")
    list_filter = ("specialty",)
    search_fields = ("name", "specialty", "phone_number")
    readonly_fields = ("uuid", "created_at", "updated_at", "created_by")
    ordering = ("name",)
