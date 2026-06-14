"""
providers/serializers.py
------------------------
Serializer for the Provider model.
"""

from rest_framework import serializers
from .models import Provider
from leads.models import Lead


class ProviderSerializer(serializers.ModelSerializer):
    """
    Full serializer for create / update / retrieve operations.
    Accepts `lead_uuid` on write; exposes it on read.
    """

    # Write-only: resolve lead by UUID instead of raw PK
    lead_uuid = serializers.UUIDField(required=False, write_only=False, allow_null=True)

    # Read-only audit info
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Provider
        fields = (
            "uuid",
            "lead_uuid",
            "name",
            "specialty",
            "email",
            "address",
            "phone_number",
            "fax_number",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "uuid",
            "created_by",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {
            "lead": {"required": False, "allow_null": True},
        }

    # ------------------------------------------------------------------
    # Field-level helpers
    # ------------------------------------------------------------------

    def get_created_by_name(self, obj):
        creator = getattr(obj, "created_by", None)
        if not creator:
            return None
        parts = [creator.first_name, creator.last_name]
        return " ".join(p for p in parts if p).strip() or creator.email or None

    # ------------------------------------------------------------------
    # Representation: always expose lead_uuid on read
    # ------------------------------------------------------------------

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.lead_id:
            data["lead_uuid"] = str(instance.lead.uuid)
        else:
            data["lead_uuid"] = None
        return data

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Provider name is required.")
        return value.strip()

    def validate(self, attrs):
        """
        Resolve lead_uuid → Lead instance.
        """
        lead_uuid = attrs.pop("lead_uuid", None)

        if lead_uuid and not attrs.get("lead"):
            try:
                attrs["lead"] = Lead.objects.get(uuid=lead_uuid)
            except Lead.DoesNotExist:
                raise serializers.ValidationError({"lead_uuid": "Lead not found."})

        return attrs


# ------------------------------------------------------------------
# Minimal read serializer (for list views — lightweight payload)
# ------------------------------------------------------------------

class ProviderListSerializer(serializers.ModelSerializer):
    lead_uuid = serializers.SerializerMethodField()

    class Meta:
        model = Provider
        fields = (
            "uuid",
            "lead_uuid",
            "name",
            "specialty",
            "email",
            "address",
            "phone_number",
            "fax_number",
            "created_at",
            "updated_at",
        )

    def get_lead_uuid(self, obj):
        return str(obj.lead.uuid) if obj.lead_id else None
