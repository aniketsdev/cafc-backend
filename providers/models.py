"""
providers/models.py
-------------------
Provider model — stores medical/care providers linked to a resident (lead).
Uses UUID for public API exposure and BigAutoField internally.
"""

import uuid
from django.db import models
from django.conf import settings


class Provider(models.Model):

    # ------------------ PRIMARY KEY ------------------
    id = models.BigAutoField(primary_key=True)

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
    )

    # ------------------ RELATIONSHIP ------------------
    lead = models.ForeignKey(
        "leads.Lead",
        on_delete=models.CASCADE,
        related_name="providers",
        null=True,
        blank=True,
    )

    # ------------------ CORE FIELDS ------------------
    name = models.CharField(max_length=255)

    specialty = models.CharField(max_length=255, null=True, blank=True)
    email = models.EmailField(null=True, blank=True) 


    address = models.TextField(null=True, blank=True)

    phone_number = models.CharField(max_length=30, null=True, blank=True)

    fax_number = models.CharField(max_length=30, null=True, blank=True)

    # ------------------ AUDIT FIELDS ------------------
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_providers",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft delete support
    deleted_at = models.DateTimeField(null=True, blank=True)

    # ------------------ META ------------------
    class Meta:
        db_table = "providers"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["lead"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.specialty or 'No Specialty'})"
