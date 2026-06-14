import logging
import uuid as uuid_lib

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile

from media.models import Media

logger = logging.getLogger(__name__)


def generate_pdf_from_html(html_content: str) -> bytes:
    """
    Convert an HTML string to PDF bytes using WeasyPrint.
    Supports CSS flexbox, base64 data-URI images, @page rules.
    """
    import weasyprint

    pdf_bytes = weasyprint.HTML(string=html_content).write_pdf()
    return pdf_bytes


def store_pdf_as_media(pdf_bytes, filename, user, consent_form=None):
    """
    Upload PDF bytes to S3 and create a Media record.

    Args:
        pdf_bytes: Raw PDF content
        filename: Display filename (e.g., "Service Agreement.pdf")
        user: The user creating this media record
        consent_form: Optional ConsentForm instance to link via metadata

    Returns:
        Media instance with S3 key populated
    """
    metadata = {
        "signed": False,
        "signed_by": None,
        "signed_date": None,
    }
    if consent_form:
        metadata["consent_form_uuid"] = str(consent_form.uuid)

    # Link to the lead via GenericForeignKey
    content_type_obj = None
    object_id = None
    if consent_form and consent_form.resident:
        try:
            content_type_obj = ContentType.objects.get_by_natural_key("leads", "lead")
            object_id = consent_form.resident.id
        except Exception:
            pass

    # Generate a unique filename to avoid S3 collisions
    unique_filename = f"{uuid_lib.uuid4()}-{filename}"

    media = Media(
        original_filename=filename,
        file_type="document",
        mime_type="application/pdf",
        file_extension="pdf",
        file_size=len(pdf_bytes),
        content_type=content_type_obj,
        object_id=object_id,
        uploaded_by=user,
        status="active",
        metadata=metadata,
    )

    # ContentFile wraps bytes; S3Boto3Storage on the FileField handles the upload
    content_file = ContentFile(pdf_bytes, name=unique_filename)
    media.file = content_file
    media.save()

    return media
