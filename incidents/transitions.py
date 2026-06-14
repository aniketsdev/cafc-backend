"""
Centralized state-machine for incident status transitions.

Each `validate_*` returns a list of human-readable error strings. An empty
list means the transition is allowed.
"""

import logging

from django.db.models import Q

from accounts.models import User
from group_home.models import GroupHomeStaffAssignment

logger = logging.getLogger(__name__)


# GroupHomeStaffAssignment.role_type values vs Role.name values for the same role.
# Two assignment paths use different spellings, so map both directions.
_ROLE_TYPE_TO_ROLE_NAME = {
    "DSP": "DSP",
    "PROGRAM_MANAGER": "Program Manager",
    "PROGRAM_COORDINATOR": "Program Coordinator",
}


def _has_active_staff_for_home(
    group_home_id,
    role_types=("DSP", "PROGRAM_MANAGER", "PROGRAM_COORDINATOR"),
):
    """
    Returns True if the home has at least ONE active staff member in ANY of the
    given role_types, via EITHER:
      (a) GroupHomeStaffAssignment row with status='ACTIVE' and matching role_type, OR
      (b) accounts.User with active=True, role.name matching, and the home in
          User.group_home (FK) or User.group_homes (M2M).
    Logs counts from both paths on every call so dev/QA failures are diagnosable.
    """
    if not group_home_id:
        logger.warning("[has-staff] group_home_id is falsy — returning False")
        return False

    role_names = [_ROLE_TYPE_TO_ROLE_NAME[r] for r in role_types if r in _ROLE_TYPE_TO_ROLE_NAME]

    gha_qs = GroupHomeStaffAssignment.objects.filter(
        group_home_id=group_home_id, role_type__in=role_types,
    )
    gha_active = gha_qs.filter(status="ACTIVE").count()
    gha_total = gha_qs.count()

    m2m_qs = User.objects.filter(
        Q(group_home_id=group_home_id) | Q(group_homes__id=group_home_id),
        role__name__in=role_names,
    ).distinct()
    m2m_active = m2m_qs.filter(active=True).count()
    m2m_total = m2m_qs.count()

    result = (gha_active > 0) or (m2m_active > 0)
    logger.info(
        f"[has-staff] group_home_id={group_home_id} roles={list(role_types)} result={result} "
        f"gha_active={gha_active} gha_total_matching_rows={gha_total} "
        f"m2m_active={m2m_active} m2m_total_matching={m2m_total}"
    )
    return result


def _has_active_dsp_for_home(group_home_id):
    """Backwards-compatible alias — checks DSP-only via the unified helper."""
    return _has_active_staff_for_home(group_home_id, role_types=("DSP",))


def validate_start(incident):
    errors = []
    if incident.status != "DRAFT":
        return [f"Cannot start from status {incident.status}"]
    if not incident.reporter_signature_id:
        errors.append("Reporter signature is required before starting the incident")
    # Relaxed 2026-05-22 per product: at least ONE active staff member on the home
    # (DSP, Program Manager, or Program Coordinator) — not all three required.
    if not _has_active_staff_for_home(incident.group_home_id):
        errors.append(
            "At least one active DSP, Program Manager, or Program Coordinator "
            "must be assigned to this group home"
        )
    # Field-level requirements (resident, incident_datetime, location,
    # incident_description) removed 2026-05-22 per product: only incident_name
    # is required to start, and that's enforced at model save by NOT NULL.
    return errors


def validate_submit_for_review(incident):
    errors = []
    if incident.status != "IN_PROGRESS":
        return [f"Cannot submit for review from status {incident.status}"]
    if not incident.reporter_signature_id:
        errors.append("Reporter signature is required")
    pm = incident.assigned_program_manager
    if not pm:
        errors.append("A Program Manager must be selected")
    else:
        active = GroupHomeStaffAssignment.objects.filter(
            user_id=pm.id, group_home_id=incident.group_home_id,
            role_type="PROGRAM_MANAGER", status="ACTIVE",
        ).exists()
        if not active:
            errors.append("Selected Program Manager is not assigned to this group home")
    return errors


def validate_send_back(incident, reason):
    if incident.status != "PM_REVIEW_PENDING":
        return [f"Cannot send back from status {incident.status}"]
    if not reason or len(reason.strip()) < 5:
        return ["Reason is required (≥5 characters)"]
    return []


def validate_pm_signoff(incident, pm_signature_media_id):
    if incident.status != "PM_REVIEW_PENDING":
        return [f"Cannot sign off from status {incident.status}"]
    return []


def validate_acknowledge(incident):
    if incident.status != "COMPLETED":
        return [f"Cannot acknowledge from status {incident.status}"]
    return []


ALLOWED_TRANSITIONS = {
    # Normal flow skips IN_PROGRESS entirely: DSP save with signature goes
    # straight to PM_REVIEW_PENDING; PM send-back returns to DRAFT.
    "DRAFT": ["PM_REVIEW_PENDING"],
    "IN_PROGRESS": ["PM_REVIEW_PENDING"],  # legacy / deprecated path
    "PM_REVIEW_PENDING": ["DRAFT", "COMPLETED"],
    "COMPLETED": ["ACKNOWLEDGED"],
    "ACKNOWLEDGED": [],
}
