from django.db import models
from django.contrib.contenttypes.fields import GenericRelation
from media.models import Media
from django.utils import timezone
from django.conf import settings
import uuid


class Appointment(models.Model):

    # ------------------ ENUM CHOICES ------------------
    CONTACT_TYPE_CHOICES = (
        ("PROVIDER", "Provider"),
        ("GUARDIAN", "Guardian"),
        ("OTHER", "Other"),
    )

    STATUS_CHOICES = (
        ("REQUESTED", "Requested"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
    )

    # ------------------ FIELDS ------------------
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
        related_name="appointments"
    )

    appointment_title = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    description = models.TextField(
        null=True,
        blank=True
    )

    appointment_date = models.DateField(
        null=True,
        blank=True
    )

    appointment_time = models.TimeField(
        null=True,
        blank=True
    )

    contact_type = models.CharField(
        max_length=20,
        choices=CONTACT_TYPE_CHOICES,
        null=True,
        blank=True
    )

    contact_name = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    contact_email = models.EmailField(
        null=True,
        blank=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="REQUESTED"
    )
    media_items = GenericRelation(
        Media,
        content_type_field='content_type',
        object_id_field='object_id',
        related_query_name='appointment'
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_appointments"
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

    action_note = models.TextField(
        null=True,
        blank=True
    )

    action_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="actioned_appointments"
    )

    action_at = models.DateTimeField(
        null=True,
        blank=True
    )
    # ------------------ META ------------------
    class Meta:
        db_table = "appointments"
        ordering = ["-appointment_date", "-appointment_time"]
        indexes = [
            models.Index(fields=["lead"]),
            models.Index(fields=["status"]),
            models.Index(fields=["appointment_date"]),
        ]

    def __str__(self):
        return f"Appointment {self.uuid} - Lead {self.lead_id}"
