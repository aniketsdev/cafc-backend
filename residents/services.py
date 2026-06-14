"""
residents/services.py
---------------------
Service layer for Care Plan report PDF generation and storage.
"""

import logging
import uuid as uuid_lib

from django.core.files.base import ContentFile

from document.services import generate_pdf_from_html
from media.models import Media
from leads.models import Lead
from .pdf_templates import generate_care_plan_report_html

logger = logging.getLogger(__name__)


def generate_and_store_report_pdf(report, user):
    """
    Generate a PDF for a CarePlanReport, upload to S3, and link it.

    Args:
        report: CarePlanReport instance (must be saved already)
        user: The user who triggered generation (for uploaded_by)

    Returns:
        Media instance, or None on failure
    """
    try:
        # Fetch the lead for DOB / age info
        lead = Lead.objects.filter(user=report.resident).first()

        # Build HTML
        html_content = generate_care_plan_report_html(report, lead)

        # Convert to PDF bytes
        pdf_bytes = generate_pdf_from_html(html_content)

        # Build filename
        resident_name = f"{report.resident.first_name}_{report.resident.last_name}".strip("_")
        filename = f"Progress_Note_{resident_name}_{report.report_month}_{report.report_year}.pdf"
        unique_filename = f"{uuid_lib.uuid4()}-{filename}"

        # Create Media record and upload to S3
        media = Media(
            original_filename=filename,
            file_type="document",
            mime_type="application/pdf",
            file_extension="pdf",
            file_size=len(pdf_bytes),
            uploaded_by=user,
            status="active",
        )
        content_file = ContentFile(pdf_bytes, name=unique_filename)
        media.file = content_file
        media.save()

        # Link to report
        report.pdf_file = media
        report.save(update_fields=["pdf_file"])

        logger.info("Generated PDF for CarePlanReport %s → Media %s", report.uuid, media.id)
        return media

    except Exception:
        logger.exception("Failed to generate PDF for CarePlanReport %s", report.uuid)
        return None
