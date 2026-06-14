from django.contrib import admin
from .models import Incident, IncidentComment

@admin.register(IncidentComment)
class IncidentCommentAdmin(admin.ModelAdmin):
    list_display = ["uuid", "incident", "role", "comment", "created_at"]
    list_filter = ["role"]
    search_fields = ["comment", "incident__uuid"]
    ordering = ["-created_at"]

admin.site.register(Incident)
