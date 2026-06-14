"""
Commit: residents/models.py
--------------------------------
Purpose:
- Implements Care Plan domain for Residents
- Supports ADLs and Goals with shift-based tracking
- Uses UUIDs for public APIs and BigInt IDs internally
- Supports daily status logging, notes, history, and archiving
"""

import uuid
from django.db import models
from django.utils import timezone
from accounts.models import User


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


# ==========================================================
# Commit 1: Care Plan Item (Master table for ADLs & Goals)
# ==========================================================
# - Represents WHAT needs to be done for a resident
# - Can be an ADL or a GOAL
# - Soft-deletable (archive support)
# - UUID is exposed to APIs instead of ID
# ==========================================================

class CarePlanItem(models.Model):

    class CarePlanType(models.TextChoices):
        ADL = "ADL", "ADL"
        GOAL = "GOAL", "Goal"

    id = models.BigAutoField(primary_key=True)

    # Public identifier (used in APIs)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False
    )

    # Resident for whom the ADL / Goal is created
    resident = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="resident_care_plan_items"
    )

    # ADL or GOAL
    type = models.CharField(
        max_length=10,
        choices=CarePlanType.choices
    )

    title = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)

    # Care staff who created this item
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_care_plan_items"
    )

    # Care staff who last updated this item
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_care_plan_items"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft delete (archive)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "care_plan_items"
        ordering = ["-created_at"]

    def archive(self):
        """Soft delete / archive the care plan item"""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def is_archived(self):
        return self.deleted_at is not None

    def __str__(self):
        return f"{self.type} - {self.title or self.uuid}"


# ==========================================================
# Commit 2: Assignment of Shifts to Care Plan Items
# ==========================================================
# - Defines WHEN an ADL / Goal should be performed
# - One care plan item can have multiple shifts
# - Prevents duplicate shift assignments
# ==========================================================

class AssignmentCarePlanItemShift(models.Model):

    class ShiftType(models.TextChoices):
        MORNING = "MORNING", "Morning"
        EVENING = "EVENING", "Evening"
        NIGHT = "NIGHT", "Night"

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    care_plan_item = models.ForeignKey(
        CarePlanItem,
        on_delete=models.CASCADE,
        related_name="assigned_shifts"
    )

    shift = models.CharField(
        max_length=10,
        choices=ShiftType.choices
    )

    class Meta:
        db_table = "assignment_care_plan_item_shifts"
        unique_together = ("care_plan_item", "shift")

    def __str__(self):
        return f"{self.care_plan_item.uuid} - {self.shift}"



# ==========================================================
# Commit 3: Daily Logs for ADLs & Goals
# ==========================================================
# - Tracks WHAT actually happened on a given day & shift
# - Stores status and notes entered by care staff
# - Acts as history for ADLs and Goals
# - Prevents duplicate logs per day per shift
# ==========================================================

class CarePlanDailyLog(models.Model):

    class StatusType(models.TextChoices):
        WORKED = "WORKED", "Worked"
        DID_NOT_WORK = "DID_NOT_WORK", "Did Not Work"
        COULD_NOT_WORK = "COULD_NOT_WORK", "Could Not Work"

    id = models.BigAutoField(primary_key=True)

    # Public identifier (used in APIs)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False
    )

    care_plan_item = models.ForeignKey(
        CarePlanItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_logs"
    )

    resident = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resident_daily_logs"
    )

    log_date = models.DateField()

    shift = models.CharField(
        max_length=10,
        choices=AssignmentCarePlanItemShift.ShiftType.choices
    )

    status = models.CharField(
        max_length=20,
        choices=StatusType.choices
    )

    note = models.TextField(blank=True, null=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_care_plan_daily_logs"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft delete for log corrections
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "care_plan_daily_logs"
        ordering = ["-log_date", "-created_at"]
        unique_together = (
            "care_plan_item",
            "resident",
            "log_date",
            "shift",
        )

    def soft_delete(self):
        """Soft delete daily log entry"""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def __str__(self):
        return (
            f"{self.care_plan_item.uuid if self.care_plan_item else 'N/A'} | "
            f"{self.log_date} | {self.shift}"
        )


# ==========================================================
# Commit X: Care Plan Monthly Report
# ==========================================================
# - Stores monthly narrative report for Goals (and later ADLs)
# - One report per resident per month/year
# - JSON-based flexible structure
# - Soft deletable
# ==========================================================

class CarePlanReport(models.Model):
    id = models.BigAutoField(primary_key=True)

    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False
    )

    resident = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="care_plan_reports"
    )

    report_month = models.PositiveSmallIntegerField()  # 1–12
    report_year = models.PositiveSmallIntegerField()

    # Stores goals / adls report data
    report_data = models.JSONField()

    generated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_care_plan_reports"
    )

    pdf_file = models.ForeignKey(
        "media.Media",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="care_plan_reports",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft delete
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "care_plan_reports"
        ordering = ["-report_year", "-report_month"]
        constraints = [
            models.UniqueConstraint(
                fields=["resident", "report_month", "report_year"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_active_report_per_resident_month",
            ),
        ]

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def __str__(self):
        return f"CarePlanReport {self.resident_id} - {self.report_month}/{self.report_year}"


# ==========================================================
# ResidentScheduledForm
# ==========================================================
# Stores a scheduled date for a named form (e.g. "Schedule 30-Day ISA").
# user = the resident (User) the form is scheduled for.
# One record per user per form_name (unique_together constraint).
# ==========================================================

class ResidentScheduledForm(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="scheduled_forms",
        help_text="The resident (User) this scheduled form belongs to.",
    )

    form_name = models.CharField(
        max_length=255,
        help_text='Name of the form, e.g. "Schedule 30-Day ISA".',
    )
    
    scheduled_date = models.DateField(
        null=True, blank=True,
        help_text='The date scheduled for the form.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "resident_scheduled_forms"
        ordering = ["-created_at"]
        unique_together = ("user", "form_name")

    def __str__(self):
        return f"{self.form_name} — user:{self.user_id} — {self.created_at}"
