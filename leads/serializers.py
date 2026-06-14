from rest_framework import serializers
from leads.models import Lead, Insurance
from group_home.models import GroupHome, GroupHomeRoom
from accounts.models import User, Address, Role
from accounts.serializers import UserSerializer, AddressSerializer
from datetime import timedelta
from django.utils import timezone
from rest_framework import serializers
from leads.models import LeadGroupHomeAssignment



#--------------------LeadSerializer----------------------------------------------
class LeadSerializer(serializers.ModelSerializer):
    referral_number = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = [
            "uuid",
            "referral_number",
            "user",
            "guardian",
            "agent",
            "guardian_relation",
            "address",
            "date_of_birth",
            "gender",
            "referral_source",
            "status",
            "reason",
            "created_at",
            "updated_at",
        ]

    def get_referral_number(self, obj):
        return f"REF-{obj.id:03d}"


#------------------InsuranceSerializer------------------------
class InsuranceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Insurance
        fields = [
            "uuid",
            "provider",
            "policy_number",
            "status",
            "created_at",
            "updated_at",
            "deleted_at",
        ]
        read_only_fields = [
            "uuid",
            "created_at",
            "updated_at",
            "deleted_at",
        ]


#-----------------InsuranceCreateSerializer----------------------------------------
class InsuranceCreateSerializer(serializers.Serializer):
    provider = serializers.CharField(
        max_length=255,
        required=False,
        allow_null=True,
        allow_blank=True
    )
    policy_number = serializers.CharField(
        max_length=255,
        required=False,
        allow_null=True,
        allow_blank=True
    )
    status = serializers.BooleanField(
        required=False,
        allow_null=True
    )


#--------------GuardianCreateSerializer-----------------------------------------------------
class GuardianCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=255)
    last_name = serializers.CharField(max_length=255)
    phone = serializers.CharField(required=False, allow_null=True)
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower()
    
#----------------------AgentCreateSerializer---------------------------------------
class AgentCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=255)
    last_name = serializers.CharField(max_length=255)
    phone = serializers.CharField(required=False, allow_null=True)
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower()
    

#---------------LeadCreateSerializer-------------------------------------------------------

class LeadCreateSerializer(serializers.Serializer):
    # -------- USER DATA --------
    first_name = serializers.CharField(max_length=255)
    last_name = serializers.CharField(max_length=255)
    email = serializers.EmailField(
    required=False,
    allow_null=True,
    allow_blank=True,
    default=None
)

    phone = serializers.IntegerField(required=False, allow_null=True)

    # -------- GUARDIAN --------
    guardian = GuardianCreateSerializer(required=False)
    guardian_uuid = serializers.UUIDField(required=False, allow_null=True)

    # -------- AGENT --------
    agent = AgentCreateSerializer(required=False)
    agent_uuid = serializers.UUIDField(required=False, allow_null=True)

    # -------- ADDRESS --------
    address = AddressSerializer(required=False)

    # -------- LEAD DATA --------
    date_of_birth = serializers.DateField(required=False)
    gender = serializers.ChoiceField(
    choices=Lead.GENDER_CHOICES,
    required=False,
    allow_null=True
)
    referral_source = serializers.CharField(
    required=False,
    allow_null=True,
    allow_blank=True
)
    status = serializers.ChoiceField(
        choices=Lead.STATUS_CHOICES,
        required=False
    )
    guardian_relation = serializers.CharField(required=False, allow_null=True)

    # -------- DOCUMENT CHECKLIST (used by backend to compute status) --------
    documents_checklist_complete = serializers.BooleanField(required=False, default=False)

    # -------- INSURANCE --------
    insurance = InsuranceCreateSerializer(required=False)


    # ---------------- VALIDATIONS ----------------
    def validate_email(self, value):
        if not value:
            return None

        email = value.lower()

        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("Lead email already exists.")

        return email



    def validate_gender(self, value):
        if not value:
            return None

        mapping = {
            "Male": "MALE",
            "Female": "FEMALE",
            "Other": "OTHER",
        }

        return mapping.get(value, value)

    
#--------------LeadDetailSerializer-----------------------------------------------------

class LeadDetailSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    guardian = UserSerializer(read_only=True)
    agent = UserSerializer(read_only=True)
    address = AddressSerializer(read_only=True)
    insurance = serializers.SerializerMethodField()
    referral_number = serializers.SerializerMethodField()
    room_number = serializers.SerializerMethodField()
    room_uuid = serializers.SerializerMethodField()
    assignment_status = serializers.SerializerMethodField()
    group_home = serializers.SerializerMethodField()
    group_home_uuid = serializers.SerializerMethodField()
    class Meta:
        model = Lead
        fields = [
            "uuid",
            "referral_number",
            "user",
            "guardian",
            "agent",
            "guardian_relation",
            "address",
            "insurance",
            "room_number",
            "room_uuid",
            "date_of_birth",
            "gender",
            "referral_source",
            "status",
            "assignment_status",
            "group_home",
            "group_home_uuid",
            "reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_referral_number(self, obj):
        return f"REF-{obj.id:03d}"

    def get_user(self, obj):
        if not obj.user_id:
          return None

        try:
            user = User.objects.get(id=int(obj.user_id))
            return UserSerializer(user, context=self.context).data
        except (User.DoesNotExist, ValueError, TypeError):
            return None
    
    def get_address(self, obj):
        address = self.context.get("address_map", {}).get(obj.address_id)
        return AddressSerializer(address).data if address else None

    def get_insurance(self, obj):
        if not obj.user_id:
            return None

        try:
            insurance = Insurance.objects.filter(
                user_id=int(obj.user_id),
                deleted_at__isnull=True
            ).first()
            return InsuranceSerializer(insurance).data if insurance else None
        except (ValueError, TypeError):
            return None

    def _get_active_assignment(self, obj):
        """Cache the active assignment lookup to avoid repeated queries."""
        if not hasattr(self, '_assignment_cache'):
            self._assignment_cache = {}
        if obj.pk not in self._assignment_cache:
            self._assignment_cache[obj.pk] = LeadGroupHomeAssignment.objects.filter(
                lead=obj,
                status="ACTIVE"
            ).select_related("room", "group_home").first()
        return self._assignment_cache[obj.pk]

    def get_room_number(self, obj):
        assignment = self._get_active_assignment(obj)
        if assignment and assignment.room:
            return assignment.room.room_number
        return None

    def get_room_uuid(self, obj):
        assignment = self._get_active_assignment(obj)
        if assignment and assignment.room:
            return str(assignment.room.uuid)
        return None

    def get_group_home(self, obj):
        assignment = self._get_active_assignment(obj)
        if assignment and assignment.group_home:
            return assignment.group_home.name
        return None

    def get_group_home_uuid(self, obj):
        assignment = self._get_active_assignment(obj)
        if assignment and assignment.group_home:
            return str(assignment.group_home.uuid)
        return None

    def get_assignment_status(self, obj):
        """
        Returns the assignment status (ACTIVE or MOVED_OUT) for residents.
        Priority: ACTIVE assignment > Most recent MOVED_OUT assignment
        If no assignment exists, returns None.
        """
        # First, check for ACTIVE assignment
        active_assignment = LeadGroupHomeAssignment.objects.filter(
            lead=obj,
            status="ACTIVE"
        ).first()
        
        if active_assignment:
            return "ACTIVE"
        
        # If no active assignment, get the most recent assignment (likely MOVED_OUT)
        latest_assignment = LeadGroupHomeAssignment.objects.filter(
            lead=obj
        ).order_by('-created_at').first()
        
        if latest_assignment:
            return latest_assignment.status  # Returns "MOVED_OUT" or "ASSIGNED"
        return None

#--------------CompleteOnboardingSerializer-----------------------------------------------------

class CompleteOnboardingSerializer(serializers.Serializer):
    group_home = serializers.SlugRelatedField(
        queryset=GroupHome.objects.filter(active=True),
        slug_field='uuid'
    )
    room = serializers.SlugRelatedField(
        queryset=GroupHomeRoom.objects.filter(is_active=True),
        slug_field='uuid',
        required=False,
        allow_null=True
    )
    check_in_date = serializers.DateField()

    def validate_check_in_date(self, value):
        min_date = timezone.now().date() + timedelta(days=0)

        if value < min_date:
            raise serializers.ValidationError(
                "Check-in date must be at least 48 hours from today."
            )
        return value
    def validate(self, data):
        room = data.get("room")
        group_home = data.get("group_home")

        if room and room.group_home_id != group_home.id:
            raise serializers.ValidationError(
                {"room": "Room does not belong to selected group home"}
            )
        
        if room:
            occupied = LeadGroupHomeAssignment.objects.filter(
                room=room,
                status="ACTIVE"
            ).exists()

            if occupied:
                raise serializers.ValidationError(
                    {"room": "This room is already occupied."}
                )

        return data
    
#----------------MoveOutSerializer-------------------------------
class MoveOutSerializer(serializers.Serializer):
    check_out_date = serializers.DateField()
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True
    )

    def validate_check_out_date(self, value):
        if value > timezone.now().date():
            raise serializers.ValidationError(
                "Check-out date cannot be in the future."
            )
        return value


#-------------ReAdmitSerializer------------------------------
class ReAdmitSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=["ACTIVE", "ASSIGNED"],
        default="ACTIVE",
        required=False
    )
    room = serializers.SlugRelatedField(
        queryset=GroupHomeRoom.objects.filter(is_active=True),
        slug_field='uuid'
    )
    group_home_uuid = serializers.SlugRelatedField(
        queryset=GroupHome.objects.filter(active=True),
        slug_field='uuid',
        source="group_home"
    )
    check_in_date = serializers.DateField(required=True)
    financial_effective_date = serializers.DateField(
        required=False,
        allow_null=True
    )

    def validate(self, data):
        room = data.get("room")
        group_home = data.get("group_home")

        if room and group_home and room.group_home_id != group_home.id:
            raise serializers.ValidationError(
                {"room": "Room does not belong to selected group home."}
            )

        if room:
            occupied = LeadGroupHomeAssignment.objects.filter(
                room=room,
                status="ACTIVE"
            ).exists()
            if occupied:
                raise serializers.ValidationError(
                    {"room": "This room is already occupied."}
                )

        return data


#-------------ResidentListSerializer------------------------------------------

class ResidentListSerializer(serializers.ModelSerializer):
    assignment_uuid = serializers.UUIDField(source="uuid")
    lead_uuid = serializers.UUIDField(source="lead.uuid")
    resident_uuid = serializers.CharField(source="lead.user.uuid")
    referral_number = serializers.SerializerMethodField()
    resident_name = serializers.SerializerMethodField()
    email = serializers.EmailField(source="lead.user.email")
    avatar_url = serializers.SerializerMethodField()
    group_home = serializers.SerializerMethodField()
    status = serializers.CharField()
    room_number = serializers.SerializerMethodField()
    check_out_date = serializers.SerializerMethodField()
    reason = serializers.SerializerMethodField()

    class Meta:
        model = LeadGroupHomeAssignment
        fields = [
            "assignment_uuid",
            "lead_uuid",
            "resident_uuid",
            "referral_number",
            "resident_name",
            "email",
            "avatar_url",
            "group_home",
            "status",
            "room_number",
            "check_out_date",
            "reason",
            "check_in_date",
            "created_at",
            "updated_at",
        ]

    def get_referral_number(self, obj):
        return f"REF-{obj.lead.id:03d}"

    def get_resident_name(self, obj):
        user = obj.lead.user
        return f"{user.first_name} {user.last_name}"

    def get_avatar_url(self, obj):
        # Resident = lead: use same avatar resolution as lead list (UserSerializer + context["avatar_urls"])
        user = getattr(obj.lead, "user", None)
        if not user:
            return None
        user_data = UserSerializer(user, context=self.context).data
        return user_data.get("avatar_url")

    def get_group_home(self, obj):
        if obj.status == "MOVED_OUT":
            return None
        return obj.group_home.name if obj.group_home else None

    def get_check_out_date(self, obj):
        if obj.status == "MOVED_OUT":
            return obj.check_out_date
        return None

    def get_reason(self, obj):
        if obj.status == "MOVED_OUT":
            return obj.reason
        return None

    def get_room_number(self, obj):
        if obj.status == "MOVED_OUT":
            return None
        if not obj.room_id:
            return None

        try:
            room = GroupHomeRoom.objects.get(id=obj.room_id)
            return room.room_number
        except GroupHomeRoom.DoesNotExist:
            return None

# ---------------- LeadDocumentSerializer ----------------
class LeadDocumentSerializer(serializers.ModelSerializer):
    """
    Serializes Lead objects in a format compatible with ResidentListSerializer,
    used when leads=true is passed to the residents API.
    """
    lead_uuid = serializers.UUIDField(source="uuid")
    resident_uuid = serializers.CharField(source="user.uuid")
    referral_number = serializers.SerializerMethodField()
    resident_name = serializers.SerializerMethodField()
    email = serializers.EmailField(source="user.email")
    avatar_url = serializers.SerializerMethodField()
    group_home = serializers.SerializerMethodField()
    lead_status = serializers.CharField(source="status")
    entry_type = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = [
            "lead_uuid", "resident_uuid", "referral_number",
            "resident_name", "email", "avatar_url",
            "group_home", "lead_status", "entry_type", "created_at",
        ]

    def get_referral_number(self, obj):
        return f"REF-{obj.id:03d}"

    def get_resident_name(self, obj):
        user = obj.user
        return f"{user.first_name} {user.last_name}"

    def get_avatar_url(self, obj):
        user = getattr(obj, "user", None)
        if not user:
            return None
        user_data = UserSerializer(user, context=self.context).data
        return user_data.get("avatar_url")

    def get_group_home(self, obj):
        # Use prefetched cache (view does prefetch_related("group_home_assignments__group_home"))
        assignments = obj.group_home_assignments.all()
        if assignments:
            return assignments[0].group_home.name
        return None

    def get_entry_type(self, obj):
        # Use prefetched cache instead of .exists() which hits DB
        assignments = obj.group_home_assignments.all()
        if assignments:
            return "RESIDENT"
        return "LEAD"


# ---------------- RejectReferralSerializer ----------------
class RejectReferralSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=True,
        allow_blank=False,
        help_text="Reason for rejecting the referral"
    )


# ---------------- TransferResidentSerializer ----------------

# leads/serializers.py

class TransferResidentSerializer(serializers.Serializer):
    group_home = serializers.SlugRelatedField(
        queryset=GroupHome.objects.filter(active=True),
        slug_field='uuid'
    )
    room = serializers.SlugRelatedField(
        queryset=GroupHomeRoom.objects.filter(is_active=True),
        slug_field='uuid',
        required=False,
        allow_null=True
    )
    check_in_date = serializers.DateField()

    def validate_check_in_date(self, value):
        min_date = timezone.now().date() + timedelta(days=0)
        if value < min_date:
            raise serializers.ValidationError(
                "Check-in date cannot be in the past."
            )
        return value
    def validate(self, data):
        room = data.get("room")
        group_home = data.get("group_home")

        if room and room.group_home_id != group_home.id:
            raise serializers.ValidationError(
                {"room": "Room does not belong to selected group home"}
            )
        if room:
            occupied = LeadGroupHomeAssignment.objects.filter(
                room=room,
                status="ACTIVE"
            ).exists()

            if occupied:
                raise serializers.ValidationError(
                    {"room": "This room is already occupied."}
                )
        return data