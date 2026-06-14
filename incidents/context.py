"""
Resident-derived incident context.

When a user creates an incident for a resident, the resident's onboarding
already records their Group Home (via Lead -> LeadGroupHomeAssignment) and
the home's assigned Program Manager + DSPs (via GroupHomeStaffAssignment).
This module stitches those relationships into one payload so the frontend
doesn't have to ask for them again.
"""

from leads.models import Lead, LeadGroupHomeAssignment
from group_home.models import GroupHomeStaffAssignment


def resident_incident_context(resident):
    """
    Resolve (group_home, program_manager_user, dsp_users) for `resident`.

    Returns a dict with keys: group_home, assigned_program_manager, dsps.
    Any field may be None / empty list when the relationship is missing.
    """
    group_home = None
    pm = None
    dsps = []

    if not resident:
        return {"group_home": None, "assigned_program_manager": None, "dsps": []}

    lead = Lead.objects.filter(user=resident).first()
    if lead:
        assignment = (
            LeadGroupHomeAssignment.objects
            .filter(lead=lead, status="ACTIVE")
            .select_related("group_home")
            .order_by("-created_at")
            .first()
        )
        if assignment:
            group_home = assignment.group_home

    if group_home:
        staff = (
            GroupHomeStaffAssignment.objects
            .filter(group_home=group_home, status="ACTIVE")
            .select_related("user")
        )
        pm_assignment = next(
            (s for s in staff if s.role_type == "PROGRAM_MANAGER"),
            None,
        )
        if pm_assignment:
            pm = pm_assignment.user
        dsps = [s.user for s in staff if s.role_type == "DSP"]

    return {
        "group_home": group_home,
        "assigned_program_manager": pm,
        "dsps": dsps,
    }


def serialize_context(ctx):
    """Render the dict from resident_incident_context() for JSON response."""
    gh = ctx["group_home"]
    pm = ctx["assigned_program_manager"]
    dsps = ctx["dsps"]
    pm_email = pm.email if pm else ""
    return {
        "group_home": (
            {"uuid": str(gh.uuid), "name": gh.name} if gh else None
        ),
        "assigned_program_manager": (
            {
                "user_uuid": str(pm.uuid),
                "first_name": pm.first_name,
                "last_name": pm.last_name,
                "email": pm.email,
            }
            if pm else None
        ),
        "program_manager_email": pm_email,
        "coordinator_email": pm_email,
        "dsps": [
            {
                "user_uuid": str(u.uuid),
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
            }
            for u in dsps
        ],
    }
