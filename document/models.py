from django.db import models
from django.utils import timezone
from leads.models import Lead  
import uuid


SIGNER_TYPE_CHOICES = (
    ("GUARDIAN", "Guardian"),
    ("AGENT", "Agent"),
)

FORM_CODE_CHOICES = (
    ("MEDICAL_DEFERMENT_FORM", "Medical Deferment Form"),
    ("HEALTH_HISTORY_FORM", "Health History Form"),
    (
        "5_AND_30_DAY_NURSING_TRANSITION_EVALUATION_FORM",
        "5 And 30 Day Nursing Transition Evaluation Form",
    ),
    ("RESIDENT_INVENTORY_LIST", "Resident Inventory List"),
    ("FIRE_SAFETY_ASSESSMENT", "Fire Safety Assessment"),
    ("HRST_MONTHLY_TRACKER", "HRST Monthly Tracker"),
    ("CAFC_BSP_TEMPLATE", "CAFC BSP (Behaviour Support Plan) Template"),
    ("MED_DISPOSAL_SHEET", "Medication Disposal Sheet"),
    ("HRC_BSP_APPROVAL_REQUEST", "HRC BSP Approval Request"),
    ("NARC_COUNT_SHEET", "Narc Count Sheet"),
    ("INCIDENT_CREATION", "Incident Creation"),
    ("MONTHLY_PROGRESS_REPORT", "Monthly Progress Report"),
    ("NH_RESIDENCY_AGREEMENT", "NH Residency Agreement"),
    ("CAFC_HOUSE_RULES", "CAFC House Rules"),
    ("BLANK_ROI", "Blank ROI"),
    ("SERVICE_AGREEMENT", "Service Agreement"),
)

FREQUENCY_TYPE_CHOICES = (
    ("ONCE", "Once"),
    ("PERIODIC", "Periodic"),
)

FORM_ENTRY_STATUS_CHOICES = (
    ("DRAFT", "Draft"),
    ("COMPLETED", "Completed"),
    ("SIGNED", "Signed"),
)


# ------------------------
# MODELS
# ------------------------

class ConsentForm(models.Model):
   
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )

    # ResidentID → FK to Lead
    resident = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name="consent_forms",
    )

    form_name = models.CharField(max_length=255)

    form_code = models.CharField(
    max_length=100,
    choices=FORM_CODE_CHOICES,
    )

    frequency_type = models.CharField(
    max_length=20,
    choices=FREQUENCY_TYPE_CHOICES,
    )

    signer_type = models.CharField(
        max_length=20,
        choices=SIGNER_TYPE_CHOICES,
        null=True,
        blank=True,
    )

    shared_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "consent_forms"
        indexes = [
            models.Index(fields=["form_code"]),
            models.Index(fields=["resident"]),
            models.Index(fields=["signer_type"]),
        ]

    def __str__(self):
        return f"{self.form_name} ({self.form_code})"


class ConsentFormEntry(models.Model):
    """
    consent_form_entries
    One ConsentForm → Many Entries
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    form = models.ForeignKey(
        ConsentForm,
        on_delete=models.CASCADE,
        related_name="entries",
    )

    version = models.CharField(max_length=50)

    form_json = models.JSONField()

    filled_at = models.DateTimeField(
        null=True,
        blank=True
    )
    next_due_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
    max_length=20,
    choices=FORM_ENTRY_STATUS_CHOICES,
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "consent_form_entries"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["form"]),
        ]

    def __str__(self):
        return f"{self.form.form_name} - v{self.version}"
