from rest_framework import serializers
from .models import AuditLog


class AuditLogListSerializer(serializers.ModelSerializer):
    staff_member = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    group_home = serializers.SerializerMethodField()
    entity_type = serializers.SerializerMethodField()
    target_user_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            "created_at",
            "staff_member",
            "role",
            "entity_type",   # 🔥 UI expects this key
            "group_home",
            "ip_address",
            "action",
            "target_user_name",
        ]

    # ✅ FIRST NAME + LAST NAME ONLY
    def get_staff_member(self, obj):
        user = obj.user
        
        # Retroactive fix: if user is missing but entity is User, try to load it
        if not user and obj.entity_type == "User" and obj.entity_id:
            from django.apps import apps
            try:
                UserModel = apps.get_model("accounts", "User")
                user = UserModel.objects.filter(id=obj.entity_id).first()
            except Exception:
                pass

        if not user:
            return "Not Assigned"

        first_name = getattr(user, "first_name", "")
        last_name = getattr(user, "last_name", "")

        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            return full_name

        return "Unknown User"

    # ✅ SAFE ROLE HANDLING (CUSTOM USER)
    def get_role(self, obj):
        user = obj.user
        
        # Retroactive fix: if user is missing but entity is User, try to load it
        if not user and obj.entity_type == "User" and obj.entity_id:
            from django.apps import apps
            try:
                UserModel = apps.get_model("accounts", "User")
                user = UserModel.objects.filter(id=obj.entity_id).first()
            except Exception:
                pass

        if not user:
            return "-"

        role = getattr(user, "role", None)
        if role:
            return getattr(role, "name", str(role))

        return "User"

    def get_group_home(self, obj):
        # ✅ Use immutable snapshot if available, otherwise fall back to ForeignKey
        if obj.log_group_home_name:
            return obj.log_group_home_name
        return obj.group_home.name if obj.group_home else "-"

    # ✅ ENTITY FIX (MATCHES UI KEY)
    def get_entity_type(self, obj):
        if not obj.entity_type:
            return "-"

        # Composite types like "Lead/Document" are already formatted — return as-is
        if "/" in obj.entity_type:
            return obj.entity_type

        return obj.entity_type.replace("_", " ").title()

    def get_target_user_name(self, obj):
        # Use immutable snapshot if available, fall back to FK
        if obj.target_user_name:
            return obj.target_user_name
        if obj.target_user:
            first = getattr(obj.target_user, "first_name", "")
            last = getattr(obj.target_user, "last_name", "")
            name = f"{first} {last}".strip()
            return name if name else None
        return None
