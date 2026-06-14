from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db.models import Q
from leads.models import Lead
from .models import IncidentNotification
from accounts.models import User
from group_home.models import GroupHomeStaffAssignment

from django.contrib.contenttypes.models import ContentType

from media.models import Media
from media.hooks import get_media_url
from media.serializers import MediaSerializer

from .models import (
    Incident,
    IncidentComment,
    IncidentMedicalFlag,
    IncidentLegalFlag,
    IncidentSocialFlag,
    IncidentVictimFlag,
)

User = get_user_model()  # resolves to accounts_user


class IncidentNotificationSerializer(serializers.ModelSerializer):

    # Read-only user details (optional)
    user_name = serializers.SerializerMethodField(read_only=True)
    user_email = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = IncidentNotification
        fields = [
            "uuid",
            "incident",
            "type",
            "user",
            "user_name",
            "user_email",
            "notify",
            "notify_date",
            "notify_time",
            "method_of_contact",
            "by_whom",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "user": {"read_only": True},   # backend assigns user
            "notify": {"required": False},
            "notify_date": {"required": False},
            "notify_time": {"required": False},
            "method_of_contact": {"required": False},
            "by_whom": {"required": False},
        }

    def get_user_name(self, obj):
        user = obj.user or self._fallback_notification_user(obj)
        if user:
            return f"{user.first_name} {user.last_name}".strip()
        return None

    def get_user_email(self, obj):
        user = obj.user or self._fallback_notification_user(obj)
        if user:
            return user.email
        return None

    def _fallback_notification_user(self, obj):
        incident = getattr(obj, "incident", None)
        if not incident:
            return None
        if obj.type == "PROGRAM_MANAGER":
            return incident.assigned_program_manager or self._staff_user_for_role(
                incident,
                GroupHomeStaffAssignment.RoleType.PROGRAM_MANAGER,
            )
        if obj.type == "ADDITIONAL_SERVICE_PROVIDER":
            return self._staff_user_for_role(
                incident,
                GroupHomeStaffAssignment.RoleType.PROGRAM_COORDINATOR,
            )
        if obj.type == "NURSING":
            return self._staff_user_for_role(
                incident,
                GroupHomeStaffAssignment.RoleType.NURSE,
            )
        return None

    def _staff_user_for_role(self, incident, role_type):
        if not getattr(incident, "group_home_id", None):
            return None
        role_names_by_type = {
            GroupHomeStaffAssignment.RoleType.PROGRAM_MANAGER: "Program Manager",
            GroupHomeStaffAssignment.RoleType.PROGRAM_COORDINATOR: "Program Coordinator",
            GroupHomeStaffAssignment.RoleType.NURSE: "Nurse",
        }
        role_name = role_names_by_type.get(role_type)

        assignment = (
            GroupHomeStaffAssignment.objects.filter(
                group_home_id=incident.group_home_id,
                role_type=role_type,
                status=GroupHomeStaffAssignment.Status.ACTIVE,
                user__active=True,
                user__deleted_at__isnull=True,
            )
            .select_related("user")
            .order_by("id")
            .first()
        )
        if assignment:
            return assignment.user

        legacy_qs = User.objects.filter(
            Q(group_home_id=incident.group_home_id) |
            Q(group_homes__id=incident.group_home_id),
            active=True,
            deleted_at__isnull=True,
        )
        if role_name:
            legacy_qs = legacy_qs.filter(Q(role__type=role_type) | Q(role__name=role_name))
        else:
            legacy_qs = legacy_qs.filter(role__type=role_type)
        return legacy_qs.distinct().order_by("id").first()

# ============================
# Incident Comment Serializer
# ============================
class IncidentCommentSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()

    class Meta:
        model = IncidentComment
        fields = [
            "uuid",
            "comment",
            "role_name",
            "created_by_name",
            "created_at",
        ]
        read_only_fields = ["uuid", "created_at"]

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
        return None

    def get_role_name(self, obj):
        if obj.role:
            return obj.role.type
        return None


# ============================
# Incident Medical Flag Serializer
# ============================
class IncidentMedicalFlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentMedicalFlag
        fields = [
            "uuid",
            "medical_type",
            "medical_other_details",
        ]


# ============================
# Incident Legal Flag Serializer
# ============================
class IncidentLegalFlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentLegalFlag
        fields = [
            "uuid",
            "legal_type",
        ]


# ============================
# Incident Social Flag Serializer
# ============================
class IncidentSocialFlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentSocialFlag
        fields = [
            "uuid",
            "social_type",
            "social_other_details",
        ]


# ============================
# Incident Victim Flag Serializer (✅ NEW)
# ============================
class IncidentVictimFlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentVictimFlag
        fields = [
            "uuid",
            "victim_type",
        ]


# ============================
# User Serializer (READ ONLY)
# ============================
class IncidentUserSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "uuid",
            "email",
            "first_name",
            "last_name",
            "avatar_url",
            "role",
        ]

    def get_role(self, obj):
        role = getattr(obj, "role", None)
        return getattr(role, "name", None)

    def get_avatar_url(self, obj):
        """Get avatar URL if exists. Uses precomputed avatar_urls from context when present."""
        try:
            avatar_urls = self.context.get("avatar_urls") if self.context else None
            if avatar_urls is not None and obj.id in avatar_urls:
                return avatar_urls[obj.id]
            if hasattr(obj, "profile_picture_id") and obj.profile_picture_id:
                return get_media_url(obj.profile_picture_id)
            content_type = ContentType.objects.get(app_label="accounts", model="user")
            avatar_media = Media.objects.filter(
                content_type=content_type,
                object_id=obj.id,
                file_type="image",
                status="active",
                alt_text__icontains="profile",
            ).order_by("-uploaded_at").first()
            if not avatar_media:
                avatar_media = Media.objects.filter(
                    content_type=content_type,
                    object_id=obj.id,
                    file_type="image",
                    status="active",
                ).order_by("-uploaded_at").first()
            if avatar_media:
                return avatar_media.get_file_url()
            return None
        except (ContentType.DoesNotExist, Exception):
            return None


# ============================
# Resident Details (with lead relation)
# ============================
class IncidentResidentDetailsSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()
    lead_uuid = serializers.SerializerMethodField()
    group_home = serializers.SerializerMethodField()
    date_of_birth = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "uuid",
            "email",
            "first_name",
            "last_name",
            "avatar_url",
            "lead_uuid",
            "group_home",
            "date_of_birth",
        ]

    def get_avatar_url(self, obj):
        """Same as IncidentUserSerializer."""
        try:
            avatar_urls = self.context.get("avatar_urls") if self.context else None
            if avatar_urls is not None and obj.id in avatar_urls:
                return avatar_urls[obj.id]
            if hasattr(obj, "profile_picture_id") and obj.profile_picture_id:
                return get_media_url(obj.profile_picture_id)
            content_type = ContentType.objects.get(app_label="accounts", model="user")
            avatar_media = Media.objects.filter(
                content_type=content_type,
                object_id=obj.id,
                file_type="image",
                status="active",
                alt_text__icontains="profile",
            ).order_by("-uploaded_at").first()
            if not avatar_media:
                avatar_media = Media.objects.filter(
                    content_type=content_type,
                    object_id=obj.id,
                    file_type="image",
                    status="active",
                ).order_by("-uploaded_at").first()
            if avatar_media:
                return avatar_media.get_file_url()
            return None
        except (ContentType.DoesNotExist, Exception):
            return None

    def get_lead_uuid(self, obj):
        """Resident (User) has reverse relation 'leads' from Lead.user."""
        try:
            leads_qs = getattr(obj, "leads", None)
            if not leads_qs:
                return None
            lead = leads_qs.first()
            return str(lead.uuid) if lead else None
        except Exception:
            return None

    def get_date_of_birth(self, obj):
        """Return the resident's date_of_birth from their Lead record (ISO format yyyy-mm-dd)."""
        try:
            leads_qs = getattr(obj, "leads", None)
            if not leads_qs:
                return None
            lead = leads_qs.first()
            if lead and lead.date_of_birth:
                return str(lead.date_of_birth)  # "YYYY-MM-DD"
            return None
        except Exception:
            return None

    def get_group_home(self, obj):
        """Resident's group home: User.group_home if set, else from lead's ACTIVE LeadGroupHomeAssignment."""
        if obj.group_home:
            return {"uuid": str(obj.group_home.uuid), "name": obj.group_home.name}
        lead = obj.leads.first()
        if not lead:
            return None
        assignment = lead.group_home_assignments.filter(status="ACTIVE").select_related("group_home").first()
        if not assignment or not assignment.group_home:
            return None
        return {"uuid": str(assignment.group_home.uuid), "name": assignment.group_home.name}


# ============================
# Incident Serializer
# ============================
class IncidentSerializer(serializers.ModelSerializer):
    lead_uuid = serializers.SerializerMethodField()
    referral_number = serializers.SerializerMethodField()
    signature_url = serializers.SerializerMethodField()
    # Accept media id from frontend (centralized Media table link); not stored on Incident model.
    signature_media_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)
    # When true, server copies the requesting user's profile signature into a new
    # incident-scoped Media row (via s3.copy_object). Lets the frontend reuse the
    # profile signature without fetching the presigned URL in the browser, which
    # fails with a CORS-masked 403 once the URL expires (~1h).
    duplicate_signature_from_profile = serializers.BooleanField(
        write_only=True, required=False, default=False
    )
    # WRITE → accept user UUIDs
    resident = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field='uuid',
        required=True
    )
    resident_uuid = serializers.CharField(
        source="resident.uuid",
        read_only=True
    )
    resident_status = serializers.SerializerMethodField()

    reported_by = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field='uuid',
        required=False,
        allow_null=True
    )
    # READ → return user details
    resident_details = IncidentResidentDetailsSerializer(
        source="resident", read_only=True
    )
    reported_by_details = IncidentUserSerializer(
        source="reported_by", read_only=True
    )
    notifications = IncidentNotificationSerializer(
        source="incident_notifications",
        many=True,
        read_only=True
    )

    comments = IncidentCommentSerializer(
        many=True,
        read_only=True
    )

    medical_flags = IncidentMedicalFlagSerializer(many=True, required=False)
    legal_flags = IncidentLegalFlagSerializer(many=True, required=False)
    social_flags = IncidentSocialFlagSerializer(many=True, required=False)
    victim_flags = IncidentVictimFlagSerializer(many=True, required=False)
    signature = serializers.SerializerMethodField()

    # Write-only FK inputs (resolve in view)
    group_home_uuid = serializers.CharField(write_only=True, required=False, allow_null=True)
    assigned_program_manager_uuid = serializers.CharField(write_only=True, required=False, allow_null=True)

    # Nested read-only representations
    group_home = serializers.SerializerMethodField()
    assigned_program_manager = serializers.SerializerMethodField()

    # Signature read fields (write-only IDs already handled by existing signature_media_id pattern; add a pm_signature one)
    pm_signature_media_id = serializers.CharField(write_only=True, required=False, allow_null=True)
    reporter_signature_url = serializers.SerializerMethodField()
    pm_signature_url = serializers.SerializerMethodField()
    pm_signature = serializers.SerializerMethodField()

    # Audit trail
    edits = serializers.SerializerMethodField()

    class Meta:
        model = Incident
        fields = [
            "uuid",
            "incident_name",
            "resident",
            "resident_uuid",
            "reported_by",
            "location",
            "region",
            "received_date",
            "incident_datetime",
            "agency_name",
            "pre_incident_notes",
            "incident_description",
            "response_action",
            "send_notification",
            "status",
            "lead_uuid",
            "referral_number",
            "signature_url",
            "signature_media_id",
            "duplicate_signature_from_profile",
            "resident_details",
            "resident_status",
            "reported_by_details",
            "notifications",
            "comments",
            "medical_flags",
            "legal_flags",
            "social_flags",
            "victim_flags",
            "signature",
            "group_home", "group_home_uuid",
            "assigned_program_manager", "assigned_program_manager_uuid",
            "program_manager_email", "coordinator_email",
            "reporter_signature_url", "pm_signature_url",
            "pm_signature", "pm_signature_media_id",
            "started_at", "submitted_for_review_at", "completed_at",
            "pm_review_notes",
            "pm_program_type",
            "pm_service_transition",
            "pm_service_transition_description",
            "pm_behavior_plan_followed",
            "pm_title",
            "edits",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "status",
            "started_at",
            "submitted_for_review_at",
            "completed_at",
        ]

    def get_group_home(self, obj):
        if not obj.group_home_id:
            return None
        return {"uuid": str(obj.group_home.uuid), "name": obj.group_home.name}

    def get_assigned_program_manager(self, obj):
        if not obj.assigned_program_manager_id:
            return None
        u = obj.assigned_program_manager
        return {
            "uuid": str(u.uuid),
            "first_name": u.first_name,
            "last_name": u.last_name,
            "email": u.email,
        }

    def _media_url(self, media):
        if not media:
            return None
        # Reuse whatever URL helper Media already exposes. Common patterns:
        if hasattr(media, "presigned_url") and callable(media.presigned_url):
            try:
                return media.presigned_url()
            except Exception:
                return None
        if hasattr(media, "get_file_url") and callable(media.get_file_url):
            try:
                return media.get_file_url()
            except Exception:
                return None
        if hasattr(media, "file") and getattr(media.file, "url", None):
            try:
                return media.file.url
            except Exception:
                return None
        return None

    def get_reporter_signature_url(self, obj):
        return self._media_url(obj.reporter_signature)

    def get_pm_signature_url(self, obj):
        return self._media_url(obj.pm_signature)

    def _media_signature_details(self, media):
        if not media:
            return None

        uploaded_by_data = None
        if media.uploaded_by:
            role_name = getattr(media.uploaded_by.role, "name", None) if hasattr(media.uploaded_by, "role") else None
            uploaded_by_data = {
                "uuid": str(media.uploaded_by.uuid),
                "email": media.uploaded_by.email,
                "first_name": media.uploaded_by.first_name,
                "last_name": media.uploaded_by.last_name,
                "role": role_name,
            }

        return {
            "id": str(media.id),
            "uploaded_at": media.uploaded_at,
            "updated_at": None,
            "uploaded_by": uploaded_by_data,
            "alt_text": media.alt_text,
        }

    def get_pm_signature(self, obj):
        """Return restricted Media details for the PM/Coordinator review signature."""
        try:
            return self._media_signature_details(obj.pm_signature)
        except Exception:
            return None

    def get_edits(self, obj):
        return [
            {
                "uuid": str(e.uuid),
                "edited_by": (
                    {"uuid": str(e.edited_by.uuid),
                     "name": f"{e.edited_by.first_name} {e.edited_by.last_name}".strip()}
                    if e.edited_by else None
                ),
                "edited_at": e.edited_at.isoformat(),
                "field_changes": e.field_changes,
            }
            for e in obj.edits.all()
        ]

    def get_signature_url(self, obj):
        """Return signed URL for the DSP/reporter signature."""
        # Prefer the explicit reporter_signature FK (most reliable).
        if obj.reporter_signature_id:
            return self._media_url(obj.reporter_signature)
        # Legacy fallback: first-ever Media linked to this incident with 'signature' in alt_text.
        try:
            content_type = ContentType.objects.get(app_label="incidents", model="incident")
            signature_media = Media.objects.filter(
                content_type=content_type,
                object_id=obj.id,
                status="active",
                alt_text__icontains="signature",
            ).order_by("uploaded_at").first()  # oldest = reporter
            if signature_media:
                return signature_media.get_file_url()
            return None
        except Exception:
            return None

    def get_signature(self, obj):
        """Return Media details for the DSP/reporter signature.

        IMPORTANT: always use the explicit reporter_signature FK.  The old
        approach (querying all active Media for the incident and taking the
        latest) breaks after the PM signs because their newer Media row
        becomes the "latest", so uploaded_by returned the PM's name and
        overwrote reporterName in the generated PDF.
        """
        try:
            # Primary: use the dedicated reporter_signature FK.
            media = obj.reporter_signature
            if not media:
                # Legacy fallback: oldest signature Media linked to the incident.
                content_type = ContentType.objects.get(app_label="incidents", model="incident")
                media = Media.objects.filter(
                    content_type=content_type,
                    object_id=obj.id,
                    alt_text__icontains="signature",
                ).order_by("uploaded_at").first()  # oldest = reporter

            return self._media_signature_details(media)
        except Exception:
            return None

    def get_lead_uuid(self, obj):
        try:
            resident = obj.resident
            if not resident:
                return None
            leads_qs = getattr(resident, "leads", None)
            if not leads_qs:
                return None
            lead = leads_qs.first()
            return str(lead.uuid) if lead else None
        except Exception:
            return None

    def get_resident_status(self, obj):
        try:
            resident = obj.resident
            if not resident:
                return None
            lead = getattr(resident, "leads", None).first()
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

    def get_referral_number(self, obj):
        try:
            resident = obj.resident
            if not resident:
                return None
            leads_qs = getattr(resident, "leads", None)
            if not leads_qs:
                return None
            lead = leads_qs.first()
            return f"REF-{lead.id:03d}" if lead else None
        except Exception:
            return None
