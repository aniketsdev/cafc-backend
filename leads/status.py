"""
Centralized lead status computation.

Status flow:
    DRAFT -> DOCS_PENDING -> UNDER_REVIEW -> ONBOARDING_IN_PROGRESS -> COMPLETED
    (any non-terminal state) -> REJECTED  (terminal, irreversible)

Rules:
    1. DRAFT           - Demographics (guardian/agent) OR insurance are missing
    2. DOCS_PENDING    - Demographics + insurance filled, document checklist incomplete
    3. UNDER_REVIEW    - All document checklist items uploaded
    4. ONBOARDING_IN_PROGRESS - All required consent forms for assigned
       guardian/agent signers are signed/completed
    5. COMPLETED       - Admin completes onboarding (assigns group home)
    6. REJECTED        - Admin rejects referral (terminal)
"""

import logging

from django.contrib.contenttypes.models import ContentType

from leads.models import Lead, Insurance

logger = logging.getLogger(__name__)

# Terminal statuses that should never be overwritten by auto-computation
TERMINAL_STATUSES = ("REJECTED", "COMPLETED")

# Each intake checklist form must have at least one uploaded document.
# The description field on Media records stores the form name.
REQUIRED_INTAKE_FORMS = (
    "Guardianship Paperwork",
    "Pending / Open Legal Cases",
    "Consent for Medical Treatment",
    "Security Protocol",
    "Safety Plan",
    "Phone / Visit List",
    "Releases of Information",
    "Respite List, if applicable",
    "Consent for Individual Therapy",
    "Client Rights",
    "Media / Phone / internet Use",
)

REQUIRED_SIGNED_CONSENT_FORM_CODES = (
    "NH_RESIDENCY_AGREEMENT",
    "CAFC_HOUSE_RULES",
    "BLANK_ROI",
)


def _check_demographics(lead):
    """Return True if the lead has a guardian or agent assigned."""
    return lead.guardian_id is not None or lead.agent_id is not None


def _check_insurance(lead):
    """Return True if the lead's user has an active insurance with provider or policy."""
    if not lead.user_id:
        return False
    insurance = Insurance.objects.filter(
        user_id=lead.user_id,
        deleted_at__isnull=True,
    ).first()
    if not insurance:
        return False
    return bool(insurance.provider or insurance.policy_number)


def _check_document_checklist(lead):
    """
    Return True if every required intake checklist form has at least one
    uploaded document.  Each form may have multiple uploads (repeating docs),
    so we check by description rather than by total count.
    """
    from media.models import Media  # deferred to avoid circular imports

    try:
        lead_ct = ContentType.objects.get(app_label="leads", model="lead")
    except ContentType.DoesNotExist:
        return False

    # Get distinct descriptions of active media for this lead
    uploaded_descriptions = set(
        Media.objects.filter(
            content_type=lead_ct,
            object_id=lead.id,
            status="active",
        )
        .exclude(description__isnull=True)
        .exclude(description="")
        .values_list("description", flat=True)
        .distinct()
    )

    # Every required form must have at least one upload
    for form_name in REQUIRED_INTAKE_FORMS:
        if form_name not in uploaded_descriptions:
            return False

    return True


def _check_consent_forms(lead):
    """
    Check consent form status for the lead.

    Returns:
        "signed"  - every required consent form for each assigned signer
                    has a COMPLETED or SIGNED entry
        "pending" - consent forms exist but not all required forms are signed
        "none"    - no consent forms at all
    """
    from document.models import ConsentForm, ConsentFormEntry

    required_signers = []
    if lead.guardian_id:
        required_signers.append("GUARDIAN")
    if lead.agent_id:
        required_signers.append("AGENT")

    if not required_signers:
        return "none"

    consent_forms = ConsentForm.objects.filter(
        resident=lead,
        deleted_at__isnull=True,
    )

    if not consent_forms.exists():
        return "none"

    for signer_type in required_signers:
        for form_code in REQUIRED_SIGNED_CONSENT_FORM_CODES:
            form = consent_forms.filter(
                signer_type=signer_type,
                form_code=form_code,
            ).first()
            if not form:
                return "pending"

            has_signed_entry = ConsentFormEntry.objects.filter(
                form=form,
                status__in=("COMPLETED", "SIGNED"),
                deleted_at__isnull=True,
            ).exists()
            if not has_signed_entry:
                return "pending"

    return "signed"


def compute_lead_status(lead):
    """
    Compute the correct lead status based on current data.

    Returns the new status string, or None if the lead is in a terminal
    state and should not be touched.
    """
    if lead.status in TERMINAL_STATUSES:
        return None

    has_demographics = _check_demographics(lead)
    has_insurance = _check_insurance(lead)

    if not (has_demographics and has_insurance):
        return "DRAFT"

    consent_status = _check_consent_forms(lead)
    if consent_status == "signed":
        return "ONBOARDING_IN_PROGRESS"

    docs_complete = _check_document_checklist(lead)
    if not docs_complete:
        return "DOCS_PENDING"

    return "UNDER_REVIEW"


def recompute_and_save(lead, request=None):
    """
    Recompute lead status and persist if changed.

    Args:
        lead: Lead instance
        request: optional DRF request (for audit logging)

    Returns:
        (changed: bool, old_status: str, new_status: str)
    """
    old_status = lead.status
    new_status = compute_lead_status(lead)

    if new_status is None or old_status == new_status:
        return False, old_status, old_status

    lead.status = new_status
    lead.save(update_fields=["status", "updated_at"])

    if request:
        try:
            from audit_logs.utils import log_action
            log_action(
                request=request,
                action="UPDATE",
                entity_type="Lead",
                entity_id=str(lead.uuid),
                message=f"Lead status auto-transitioned: {old_status} -> {new_status}",
            )
        except Exception:
            logger.exception("Failed to log status transition audit")

    return True, old_status, new_status
