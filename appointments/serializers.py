from rest_framework import serializers
from .models import Appointment
from leads.models import Lead
from group_home.models import GroupHome
from media.serializers import MediaMinimalSerializer
from accounts.serializers import UserSerializer

class LeadDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        exclude = ["id"]
        
#---------------------AppointmentSerializer-------------------------------------------------------
class AppointmentSerializer(serializers.ModelSerializer):
    lead_uuid = serializers.UUIDField(required=False)
    referral_number = serializers.SerializerMethodField()
    resident_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    created_by_email = serializers.EmailField(
        source="created_by.email",
        read_only=True
    )
    created_by_name = serializers.SerializerMethodField()
    resident_status = serializers.SerializerMethodField()

    lead_detail = LeadDetailSerializer(source="lead", read_only=True)
    media = MediaMinimalSerializer(
        source="media_items",
        many=True,
        read_only=True
    )

    room = serializers.SerializerMethodField()
    group_home = serializers.SerializerMethodField()
    
    class Meta:
        model = Appointment
        fields = (
            "uuid", "lead", "lead_uuid", "referral_number", "resident_name", "avatar_url", "resident_status",
            "appointment_title", "description", "appointment_date", "appointment_time",
            "contact_type", "contact_name", "contact_email", "status",
            "lead_detail", "media",
            "room", "group_home",
            "created_by", "created_by_email", "created_by_name", "created_at", "updated_at",
            "deleted_at", "action_note", "action_by", "action_at",
        )
        read_only_fields = (
            "uuid",
            "created_by",
            "created_at",
            "updated_at",
            "action_by",
            "action_at",
        )
        extra_kwargs = {
            "lead": {"required": False, "allow_null": True},
            "contact_email": {"required": False, "allow_null": True, "allow_blank": True},  # ← CHANGED: email is now optional
        }

    def get_referral_number(self, obj):
        try:
            return f"REF-{obj.lead.id:03d}" if obj.lead else None
        except Exception:
            return None

    def get_resident_name(self, obj):
        return f"{obj.lead.user.first_name} {obj.lead.user.last_name}"

    def get_avatar_url(self, obj):
        """Resident = lead's user. Reuse UserSerializer avatar resolution (and context avatar_urls if set)."""
        user = getattr(getattr(obj, "lead", None), "user", None)
        if not user:
            return None
        user_data = UserSerializer(user, context=self.context).data
        return user_data.get("avatar_url")

    def get_resident_status(self, obj):
        try:
            lead = getattr(obj, "lead", None)
            if not lead:
                return None
            # Check active assignment first
            active = lead.group_home_assignments.filter(status="ACTIVE").first()
            if active:
                return "ACTIVE"
            # Get latest assignment
            latest = lead.group_home_assignments.order_by('-created_at').first()
            if latest:
                return latest.status
            return None
        except Exception:
            return None

    def get_created_by_name(self, obj):
        creator = getattr(obj, "created_by", None)
        if not creator:
            return None
        parts = [creator.first_name, creator.last_name]
        return " ".join(p for p in parts if p).strip() or creator.email or None

    
    # -------------------------
    # ROOM & GROUP HOME LOGIC
    # -------------------------

    def _get_active_assignment(self, obj):
        """
        Uses prefetched active_assignment if available.
        Falls back to DB only if not prefetched (safe for other APIs).
        """
        lead = getattr(obj, "lead", None)
        if not lead:
            return None

        # ✅ Prefetched path (no DB hit)
        active_list = getattr(lead, "active_assignment", None)
        if active_list is not None:
            return active_list[0] if active_list else None

        # ⚠️ Fallback (only if someone forgot prefetch)
        return (
            lead.group_home_assignments
            .filter(status="ACTIVE")
            .select_related("room", "group_home")
            .first()
        )

    def get_room(self, obj):
        assignment = self._get_active_assignment(obj)

        if not assignment or not assignment.room:
            return None

        room = assignment.room

        return {
            "uuid": str(room.uuid),
            "room_number": room.room_number,
            "is_active": room.is_active,
            "is_occupied": room.is_occupied,
        }

    def get_group_home(self, obj):
        assignment = self._get_active_assignment(obj)

        if not assignment or not assignment.group_home:
            return None

        gh = assignment.group_home

        return {
            "uuid": str(gh.uuid),
            "name": gh.name,
        }

    # -------------------------
    # VALIDATIONS
    # -------------------------



    def validate_contact_email(self, value):
        if value and "@" not in value:
            raise serializers.ValidationError("Enter a valid email address.")
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Return lead_uuid in responses
        if instance.lead:
            data["lead_uuid"] = str(instance.lead.uuid)
        else:
            data["lead_uuid"] = None
        return data

    def validate(self, attrs):
        """
        Cross-field validation + lead_uuid resolution
        """
        # Resolve lead_uuid to Lead instance
        lead_uuid = attrs.pop("lead_uuid", None)
        if lead_uuid and not attrs.get("lead"):
            try:
                attrs["lead"] = Lead.objects.get(uuid=lead_uuid)
            except Lead.DoesNotExist:
                raise serializers.ValidationError({"lead_uuid": "Lead not found."})

        # Only require lead on create (not partial update)
        if not attrs.get("lead") and not self.instance:
            raise serializers.ValidationError({"lead": "Lead is required."})

        contact_type = attrs.get("contact_type")
        # ← REMOVED: contact_email required check — email is now optional

        contact_name = attrs.get("contact_name")

        if contact_type == "OTHER" and not contact_name:
            raise serializers.ValidationError({
                "contact_name": "Contact name is required for OTHER."
            })

        return attrs

#---------------------AppointmentListSerializer-------------------------------------------------------
class AppointmentListSerializer(serializers.ModelSerializer):
    referral_number = serializers.SerializerMethodField()
    resident_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    resident_status = serializers.SerializerMethodField()
    media = MediaMinimalSerializer(
        source="media_items",
        many=True,
        read_only=True
    )

    class Meta:
        model = Appointment
        fields = [
            "uuid", "lead", "referral_number", "appointment_title", "description",
            "appointment_date", "appointment_time", "contact_type", "contact_name", "contact_email",
            "status", "created_by", "created_at", "updated_at", "deleted_at",
            "action_note", "action_by", "action_at",
            "resident_name", "avatar_url", "media", "resident_status",
        ]

    def get_referral_number(self, obj):
        try:
            return f"REF-{obj.lead.id:03d}" if obj.lead else None
        except Exception:
            return None

    def get_resident_name(self, obj):
        return f"{obj.lead.user.first_name} {obj.lead.user.last_name}"

    def get_avatar_url(self, obj):
        """Resident = lead's user. Reuse UserSerializer avatar resolution (and context avatar_urls if set)."""
        user = getattr(getattr(obj, "lead", None), "user", None)
        if not user:
            return None
        user_data = UserSerializer(user, context=self.context).data
        return user_data.get("avatar_url")

    def get_resident_status(self, obj):
        try:
            lead = getattr(obj, "lead", None)
            if not lead:
                return None
            # Check active assignment first
            active = lead.group_home_assignments.filter(status="ACTIVE").first()
            if active:
                return "ACTIVE"
            # Get latest assignment
            latest = lead.group_home_assignments.order_by('-created_at').first()
            if latest:
                return latest.status
            return None
        except Exception:
            return None

