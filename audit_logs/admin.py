from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    # --------------------
    # Table columns
    # --------------------
    list_display = (
        "created_at",     # Date & Time
        "get_staff_member",
        "get_role",
        "entity_type",    # Entity
        "group_home",     # Group Home
        "ip_address",     # IP Address
    )

    # --------------------
    # Filters (top-right)
    # --------------------
    list_filter = (
        "entity_type",
        "group_home",
        "created_at",
    )

    # --------------------
    # Search (top search bar)
    # --------------------
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "entity_type",
        "ip_address",
    )

    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    # --------------------
    # Read-only protection
    # --------------------
    readonly_fields = (
        "user",
        "action",
        "entity_type",
        "entity_id",
        "group_home",
        "message",
        "ip_address",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # --------------------
    # Custom display methods
    # --------------------
    @admin.display(description="Staff Member")
    def get_staff_member(self, obj):
        if obj.user:
            full_name = obj.user.get_full_name()
            return full_name if full_name else obj.user.username
        return "Not Assigned"

    @admin.display(description="Role")
    def get_role(self, obj):
        if obj.user and hasattr(obj.user, "role") and obj.user.role:
            return obj.user.role.name
        return "-"
