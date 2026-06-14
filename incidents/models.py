import uuid
from django.db import models
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from accounts.models import Role


# ============================
# Time Zone Choices
# ============================
TIME_ZONE_CHOICES = [
    ("ET", "Eastern Time"),
    ("CT", "Central Time"),
    ("MT", "Mountain Time"),
    ("PT", "Pacific Time"),
    ("AZ", "Arizona Time"),
    ("AK", "Alaska Time"),
]


# ============================
# Incident Model
# ============================
class Incident(models.Model):

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        PM_REVIEW_PENDING = "PM_REVIEW_PENDING", "Program Manager Review Pending"
        COMPLETED = "COMPLETED", "Completed"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"

    class PMServiceTransition(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"

    class PMBehaviorPlanFollowed(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"
        NA = "N_A", "N/A"

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    resident = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resident_incidents",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reported_incidents",
    )
    group_home = models.ForeignKey(
        "group_home.GroupHome",
        on_delete=models.PROTECT, null=True, blank=True,
        related_name="incidents",
    )
    assigned_program_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incidents_to_review",
    )

    program_manager_email = models.EmailField(null=True, blank=True)
    coordinator_email = models.EmailField(null=True, blank=True)

    reporter_signature = models.ForeignKey(
        "media.Media", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reporter_for_incidents",
    )
    pm_signature = models.ForeignKey(
        "media.Media", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pm_for_incidents",
    )

    incident_name = models.CharField(max_length=255, null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    region = models.CharField(max_length=255, null=True, blank=True)

    received_date = models.DateField(null=True, blank=True)
    incident_datetime = models.DateTimeField(null=True, blank=True)

    agency_name = models.CharField(max_length=255, null=True, blank=True)

    pre_incident_notes = models.TextField(null=True, blank=True)
    incident_description = models.TextField(null=True, blank=True)
    response_action = models.TextField(null=True, blank=True)

    send_notification = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    started_at = models.DateTimeField(null=True, blank=True)
    submitted_for_review_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # PM Review/Follow-up fields
    pm_review_notes = models.TextField(null=True, blank=True)
    pm_program_type = models.CharField(max_length=255, null=True, blank=True)
    pm_service_transition = models.CharField(
        max_length=4, choices=PMServiceTransition.choices, null=True, blank=True,
    )
    pm_service_transition_description = models.TextField(null=True, blank=True)
    pm_behavior_plan_followed = models.CharField(
        max_length=4, choices=PMBehaviorPlanFollowed.choices, null=True, blank=True,
    )
    pm_title = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Incident {self.uuid}"


# ============================
# Incident Medical Flags
# ============================
class IncidentMedicalFlag(models.Model):

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="medical_flags"
    )
    medical_type = models.CharField(max_length=255)
    medical_other_details = models.TextField(blank=True, default="")

    def __str__(self):
        return self.medical_type


# ============================
# Incident Legal Flags
# ============================
class IncidentLegalFlag(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="legal_flags"
    )
    legal_type = models.CharField(max_length=255)

    def __str__(self):
        return self.legal_type


# ============================
# Incident Social Flags
# ============================
class IncidentSocialFlag(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="social_flags"
    )
    social_type = models.CharField(max_length=255)
    social_other_details = models.TextField(blank=True, default="")

    def __str__(self):
        return self.social_type


# ============================
# Incident Victim Flags (✅ NEW)
# ============================
class IncidentVictimFlag(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="victim_flags"
    )
    victim_type = models.CharField(max_length=255)

    class Meta:
        db_table = "incident_victim_flags"

    def __str__(self):
        return self.victim_type

# ============================
# Incident Notification Model
# ============================

class IncidentNotification(models.Model):

    NOTIFICATION_TYPE_CHOICES = [
        ("STAFF", "Staff"),
        ("SERVICE_COORDINATOR", "Service Coordinator"),
        ("PROGRAM_MANAGER", "Program Manager"),
        ("GUARDIAN", "Guardian"),
        ("AGENT", "Agent"),
        ("NURSING", "Nursing"),
        ("ADDITIONAL_SERVICE_PROVIDER", "Additional Service Provider"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    incident = models.ForeignKey(
        "Incident",
        on_delete=models.CASCADE,
        related_name="incident_notifications"
    )

    type = models.CharField(
        max_length=50,
        choices=NOTIFICATION_TYPE_CHOICES
    )

    user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name="incident_notifications"
)


    notify = models.BooleanField(default=False)
    notify_date = models.DateField(null=True, blank=True)
    notify_time = models.TimeField(null=True, blank=True)
    method_of_contact = models.CharField(max_length=255, null=True, blank=True)
    by_whom = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "incident_notifications"

    def __str__(self):
        return f"{self.type} - {self.user.email}"

class IncidentComment(models.Model):

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="comments"
    )

    # ✅ comment text
    comment = models.TextField()

    # ✅ user (user_id)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_comments"
    )

    # ✅ role (role_id)
    role = models.ForeignKey(
        Role,   # make sure Role is imported
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_comments"
    )

    # ✅ timestamps
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "incident_comments"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role.name if self.role else 'No Role'} comment on Incident {self.incident.uuid}"


class IncidentEdit(models.Model):
    """
    Append-only audit log of saves to an incident from IN_PROGRESS onward.
    One row per save (PUT/PATCH) call, not per field-blur. JSON body captures
    the diff so we can render "what the DSP wrote" vs "what the PM finalized".
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    incident = models.ForeignKey(
        Incident, on_delete=models.CASCADE, related_name="edits",
    )
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="incident_edits",
    )
    edited_at = models.DateTimeField(default=timezone.now)
    field_changes = models.JSONField(default=dict)

    class Meta:
        db_table = "incident_edits"
        ordering = ["edited_at"]

    def __str__(self):
        return f"Edit on {self.incident_id} by {self.edited_by_id} @ {self.edited_at}"
