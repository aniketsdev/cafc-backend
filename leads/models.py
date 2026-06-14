from django.db import models
from accounts.models import User, Address
from group_home.models import GroupHomeRoom
from django.db import models
from django.utils import timezone
from django.conf import settings
import uuid

#------------------Lead Table-------------------------------------------

class Lead(models.Model):
    GENDER_CHOICES = (
        ("MALE", "Male"),
        ("FEMALE", "Female"),
        ("OTHER", "Other"),
        ("UNKNOWN", "Unknown"),
    )

    STATUS_CHOICES = (
        ("DRAFT", "Draft"),
        ("UNDER_REVIEW", "Under Review"),
        ("DOCS_PENDING", "Docs Pending"),
        ("ONBOARDING_IN_PROGRESS", "Onboarding In Progress"),
        ("COMPLETED", "Completed"),
        ("REJECTED", "Rejected"),
    )

    id = models.BigAutoField(primary_key=True)

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="leads",
    )

    guardian = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guarded_leads"
    )

    agent = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_leads"
    )

    guardian_relation = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    
    address = models.ForeignKey(          
        Address,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leads"
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True
    )

    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, null=True, blank=True)
    referral_source = models.CharField(max_length=255, null=True, blank=True)

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, null=True, blank=True)
    reason = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "leads"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Lead {self.uuid}"


#--------------------Insurance Table----------------------

class Insurance(models.Model):
    id = models.BigAutoField(primary_key=True)

    uuid = models.CharField(
        max_length=36,
        unique=True,
        default=uuid.uuid4,
        editable=False
    )

    user = models.ForeignKey(     
        User,
        on_delete=models.CASCADE,
        related_name="insurances",
    )  # REQUIRED = TRUE

    provider = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    policy_number = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    status = models.BooleanField(
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        null=True,
        blank=True
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        null=True,
        blank=True
    )

    deleted_at = models.DateTimeField(
        null=True,
        blank=True
    )

    class Meta:
        db_table = "insurance"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Insurance #{self.id} - User {self.user_id}"


#--------------LeadGroupHomeAssignment----------------------------------------------

class LeadGroupHomeAssignment(models.Model):
    
    STATUS_CHOICES = (
        ("ASSIGNED", "Assigned"),
        ("ACTIVE", "Active"),
        ("MOVED_OUT", "Moved Out"),
    )

    ADMISSION_TYPE_CHOICES = (
        ("ASSISTED_LIVING", "Assisted Living"),
        ("RESPITE", "Respite"),
        ("HOSPICE", "Hospice"),
    )

    OCCUPANCY_TYPE_CHOICES = (
        ("PRIVATE", "Private"),
        ("SHARED", "Shared"),
        ("SECOND_RESIDENT", "2nd Resident"),
    )

    id = models.BigAutoField(primary_key=True)

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True
    )

    lead = models.ForeignKey(
        "leads.Lead",
        on_delete=models.CASCADE,
        related_name="group_home_assignments"
    )

    group_home = models.ForeignKey(
        "group_home.GroupHome",
        on_delete=models.PROTECT,
        related_name="lead_assignments"
    )

    room = models.ForeignKey(
        GroupHomeRoom,
        on_delete=models.PROTECT,
        related_name="assignments",
        null=True,
        blank=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="ACTIVE"
    )

    admission_type = models.CharField(
        max_length=20,
        choices=ADMISSION_TYPE_CHOICES,
        null=True,
        blank=True
    )

    reason = models.TextField(
        null=True,
        blank=True
    )
    
    check_in_date = models.DateField(null=True, blank=True)
    check_out_date = models.DateField(null=True, blank=True)
    financial_effective_date = models.DateField(null=True, blank=True)

    occupancy_type = models.CharField(
        max_length=20,
        choices=OCCUPANCY_TYPE_CHOICES,
        null=True,
        blank=True
    )
    resubmitted = models.BooleanField(default=False)

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_group_homes"
    )

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "lead_group_home_assignments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["lead"]),
            models.Index(fields=["group_home"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["lead"],
                condition=models.Q(status="ACTIVE"),
                name="unique_active_assignment_per_lead"
            ),
            models.UniqueConstraint(
                fields=["room"],
                condition=models.Q(status="ACTIVE"),
                name="unique_active_assignment_per_room"
            ),
        ]

    def __str__(self):
        return f"Lead {self.lead_id} → GroupHome {self.group_home_id} ({self.status})"
