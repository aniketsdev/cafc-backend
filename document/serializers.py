from rest_framework import serializers
from document.models import ConsentForm, ConsentFormEntry
from leads.models import Lead

class ConsentFormSerializer(serializers.ModelSerializer):
    resident_uuid = serializers.UUIDField(
        source="resident.uuid",
        read_only=True
    )
    status = serializers.SerializerMethodField()
    next_due_at = serializers.SerializerMethodField()

    class Meta:
        model = ConsentForm
        fields = [
            "uuid",
            "resident_uuid",
            "form_name",
            "form_code",
            "frequency_type",
            "signer_type",
            "status",
            "next_due_at",
            "shared_at",
            "created_at",
            "updated_at",
        ]

    def get_status(self, obj):
        latest_entry = obj.entries.filter(
            deleted_at__isnull=True
        ).order_by("-created_at").first()

        return latest_entry.status if latest_entry else None
    
    def get_next_due_at(self, obj):
        latest_entry = obj.entries.filter(
            deleted_at__isnull=True
        ).order_by("-created_at").first()

        return latest_entry.next_due_at if latest_entry else None
#---------------------------------------------------------------------------------------------------
class ConsentFormEntrySerializer(serializers.ModelSerializer):
    form_uuid = serializers.UUIDField(
        source="form.uuid",
        read_only=True
    )

    class Meta:
        model = ConsentFormEntry
        fields = [
            "uuid",
            "form_uuid",
            "version",
            "form_json",
            "filled_at",
            "next_due_at",
            "status",
            "created_at",
            "updated_at",
        ]
#---------------------------------------------------------------------------------------------------
class ConsentFormCreateSerializer(serializers.Serializer):
    resident_uuid = serializers.UUIDField()
    form_name = serializers.CharField(max_length=255)
    form_code = serializers.CharField(max_length=100)
    frequency_type = serializers.CharField(max_length=20)
    status = serializers.ChoiceField(choices=("DRAFT", "COMPLETED", "SIGNED"))
    form_json = serializers.JSONField(required=False, default=dict)
    signer_type = serializers.CharField(max_length=20, required=False, allow_null=True)

    next_due_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_resident_uuid(self, value):
        if not Lead.objects.filter(uuid=value).exists():
            raise serializers.ValidationError("Resident not found")
        return value
#---------------------------------------------------------------------------------------------------
class ConsentFormUpdateSerializer(serializers.Serializer):
    """
    Used ONLY for updating consent form via form_uuid.
    Backend decides whether to update or create a new version.
    """

    status = serializers.ChoiceField(
        choices=("DRAFT", "COMPLETED", "SIGNED")
    )

    form_json = serializers.JSONField(
        required=False,
        default=dict
    )
    next_due_at = serializers.DateTimeField(
        required=False,
        allow_null=True
    )

    def validate_form_json(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("form_json must be a JSON object")
        return value
#---------------------------------------------------------------------------------------
class ConsentFormShareSerializer(serializers.Serializer):
    recipient_email = serializers.EmailField(required=False, allow_null=True)
    recipient_emails = serializers.ListField(child=serializers.EmailField(), required=False)
    recipient_name = serializers.CharField(max_length=255, required=False, allow_null=True)
    media_uuid = serializers.UUIDField(required=False, allow_null=True)
    pdf_base64 = serializers.CharField(required=False, write_only=True)
    pdf_filename = serializers.CharField(max_length=255, required=False, write_only=True)
    html_content = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="HTML string to convert to PDF on the server. "
                  "If provided, backend generates PDF, uploads to S3, "
                  "and creates a Media record automatically."
    )
    recipient_type = serializers.ChoiceField(
        choices=(("GUARDIAN", "Guardian"), ("AGENT", "Agent"), ("CUSTOM", "Custom")),
        required=False,
        allow_null=True,
        help_text="Portal role to share with. Stamps ConsentForm.signer_type "
                  "so the document is visible ONLY in the matching portal."
    )

#---------------------------------------------------------------------------------------
class ConsentFormDetailSerializer(serializers.ModelSerializer):
    resident_uuid = serializers.UUIDField(
        source="resident.uuid",
        read_only=True
    )
    entries = ConsentFormEntrySerializer(many=True, read_only=True)

    class Meta:
        model = ConsentForm
        fields = [
            "uuid",
            "resident_uuid",
            "form_name",
            "form_code",
            "frequency_type",
            "signer_type",
            "entries",
            "shared_at",
            "created_at",
            "updated_at",
        ]
