"""
Email helpers for the incident workflow.

Uses Django's default email backend (configured in settings.py — for prod
this is wired to SendGrid via `django-anymail` or the `sendgrid` package
already in requirements.txt; for tests Django automatically uses locmem).
"""

import logging
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

from incidents.pdf_report import build_incident_pdf

logger = logging.getLogger(__name__)


def _frontend_acknowledge_url(incident):
    base = getattr(settings, "FRONTEND_BASE_URL", None) or ""
    return f"{base.rstrip('/')}/portal/incidents/{incident.uuid}/acknowledge"


def _user_display_name(user):
    if not user:
        return ""
    return f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email


def send_completion_email(incident, recipient_user, signed_off_by_user=None):
    """
    Send the 'incident completed' notification to a single recipient.
    Returns (ok: bool, info: str). Never raises — errors are logged.
    """
    if not recipient_user or not recipient_user.email:
        return False, "no_email"

    resident_name = "(unknown)"
    if incident.resident:
        resident_name = f"{incident.resident.first_name} {incident.resident.last_name}".strip()

    signature_user = None
    try:
        signature_user = incident.pm_signature.uploaded_by if incident.pm_signature else None
    except Exception:
        signature_user = None
    pm_name = (
        _user_display_name(signed_off_by_user)
        or _user_display_name(signature_user)
        or _user_display_name(incident.assigned_program_manager)
    )

    ctx = {
        "recipient_name": (
            f"{recipient_user.first_name} {recipient_user.last_name}".strip()
            or recipient_user.email
        ),
        "resident_name": resident_name,
        "incident_datetime": (
            incident.incident_datetime.strftime("%-m-%d-%Y %I:%M %p")
            if incident.incident_datetime else ""
        ),
        "location": incident.location or "",
        "group_home_name": incident.group_home.name if incident.group_home else "",
        "pm_name": pm_name,
        "acknowledge_url": _frontend_acknowledge_url(incident),
    }

    subject = f"Incident Report Completed — {ctx['resident_name']} ({ctx['incident_datetime']})"
    try:
        html_body = render_to_string("incidents/emails/incident_completed.html", ctx)
    except Exception as e:
        logger.error(f"[email] template render FAILED incident={incident.uuid} err={e}")
        return False, f"template_error:{e}"

    try:
        msg = EmailMessage(
            subject=subject, body=html_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@cafc.example"),
            to=[recipient_user.email],
        )
        msg.content_subtype = "html"  # Set primary content type to HTML

        try:
            pdf_bytes = build_incident_pdf(incident)
            safe_resident = (resident_name or "incident").replace(" ", "_").replace("/", "-")
            msg.attach(
                f"incident_report_{safe_resident}_{incident.uuid}.pdf",
                pdf_bytes,
                "application/pdf",
            )
        except Exception as e:
            logger.error(
                f"[email] PDF attach FAILED incident={incident.uuid} "
                f"err={type(e).__name__}: {e}"
            )
            # Continue and send the email without the attachment rather than fail.

        msg.send(fail_silently=False)
        logger.info(
            f"[email] incident_completed sent incident={incident.uuid} to={recipient_user.email}"
        )
        return True, "sent"
    except Exception as e:
        logger.error(
            f"[email] incident_completed FAILED incident={incident.uuid} "
            f"to={recipient_user.email} err={e}"
        )
        return False, str(e)
