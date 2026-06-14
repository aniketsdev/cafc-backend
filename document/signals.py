import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)


@receiver(post_save, sender="media.Media")
def sync_media_sign_to_consent_form(sender, instance, **kwargs):
    """
    When a Media record is saved with metadata.signed=True and
    metadata.consent_form_uuid set:
    1. Update the ConsentFormEntry status to SIGNED
    2. Store signature data in form_json so the PDF can be regenerated
       with the guardian/agent signature embedded.
    """
    try:
        metadata = instance.metadata or {}
        consent_form_uuid = metadata.get("consent_form_uuid")
        is_signed = metadata.get("signed", False)

        if not consent_form_uuid or not is_signed:
            return

        # Lazy import to avoid circular dependency
        from document.models import ConsentForm

        form = ConsentForm.objects.filter(
            uuid=consent_form_uuid,
            deleted_at__isnull=True,
        ).first()

        if not form:
            return

        latest_entry = (
            form.entries
            .filter(deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )

        if not latest_entry or latest_entry.status == "SIGNED":
            return

        # Single `now` used for both entry and parent form so their
        # updated_at values are identical to the microsecond.
        now = timezone.now()

        # Update entry status
        latest_entry.status = "SIGNED"

        # Embed signature info into form_json so frontend can
        # regenerate the PDF with signature visible in the document
        form_json = latest_entry.form_json or {}
        signature_data = metadata.get("signature_data")
        signer_name = metadata.get("signer_name")
        signed_date = metadata.get("signed_date")

        # Determine the correct field names based on form_code.
        # House Rules uses snake_case fields (guardian_signature, guardian_date).
        # Other forms (NH Residency Agreement, etc.) use camelCase fields
        # (legalGuardianSignature, legalGuardianSignatureDate, legalGuardianPrintName).
        form_code = getattr(form, "form_code", "") or ""

        if form_code == "CAFC_HOUSE_RULES":
            if signature_data:
                form_json["guardian_signature"] = signature_data
                form_json["guardian_signatureMethod"] = "DRAW"
            if signed_date:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(signed_date)
                    form_json["guardian_date"] = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    form_json["guardian_date"] = signed_date
        else:
            # Default: NH Residency Agreement and similar forms
            if signature_data:
                form_json["legalGuardianSignature"] = signature_data
            if signer_name:
                form_json["legalGuardianPrintName"] = signer_name
            if signed_date:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(signed_date)
                    form_json["legalGuardianSignatureDate"] = dt.strftime("%m/%d/%Y")
                except (ValueError, TypeError):
                    form_json["legalGuardianSignatureDate"] = signed_date

        latest_entry.form_json = form_json
        latest_entry.updated_at = now
        latest_entry.save(update_fields=["status", "form_json", "updated_at"])

        # Advance the parent ConsentForm.updated_at so the Admin Portal
        # "Last Updated" column reflects the signing event. Mirrors the
        # explicit write pattern used by the share and version-update flows
        # (see document/views.py). Minimal update_fields — nothing else
        # on the row is touched, and there is no post_save handler on
        # ConsentForm so this cannot cascade.
        form.updated_at = now
        form.save(update_fields=["updated_at"])

        try:
            from leads.status import recompute_and_save

            recompute_and_save(form.resident)
        except Exception:
            logger.exception(
                "Failed to recompute lead status after consent form signing"
            )

        logger.info(
            "Consent form %s entry marked SIGNED via media signal",
            consent_form_uuid,
        )
    except Exception:
        logger.exception(
            "Error in sync_media_sign_to_consent_form signal"
        )
