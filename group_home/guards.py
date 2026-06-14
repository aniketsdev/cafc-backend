"""
Pre-flight checks for deactivate/delete operations.

Each function returns (can_delete: bool, blockers: list[dict]).
Blocker shape: {"type": "assignment" | "open_incident", "id": str, "label": str}.
"""

from group_home.models import GroupHomeStaffAssignment
from incidents.models import Incident

NON_TERMINAL_STATUSES = ["DRAFT", "IN_PROGRESS", "PM_REVIEW_PENDING"]


def group_home_blockers(group_home):
    blockers = []

    for a in group_home.staff_assignments.filter(status="ACTIVE").select_related("user"):
        blockers.append({
            "type": "assignment",
            "id": str(a.uuid),
            "label": f"{a.user.first_name} {a.user.last_name} ({a.role_type})",
        })

    for inc in Incident.objects.filter(
        group_home=group_home, status__in=NON_TERMINAL_STATUSES, deleted_at__isnull=True,
    ):
        blockers.append({
            "type": "open_incident",
            "id": str(inc.uuid),
            "label": f"Incident on {inc.incident_datetime or inc.created_at.date()} ({inc.status})",
        })

    return len(blockers) == 0, blockers


def user_blockers(user):
    blockers = []

    assignments = list(
        user.group_home_staff_assignments
            .filter(status="ACTIVE")
            .select_related("group_home")
    )

    dsp_home_ids = {a.group_home_id for a in assignments if a.role_type == "DSP"}
    homes_with_other_dsp = set(
        GroupHomeStaffAssignment.objects
            .filter(group_home_id__in=dsp_home_ids, role_type="DSP", status="ACTIVE")
            .exclude(user=user)
            .values_list("group_home_id", flat=True)
    ) if dsp_home_ids else set()

    for a in assignments:
        if a.role_type == "DSP":
            if a.group_home_id in homes_with_other_dsp:
                continue  # not the last DSP — allow deactivation
            blockers.append({
                "type": "assignment",
                "id": str(a.uuid),
                "label": f"{a.group_home.name} ({a.role_type})",
                "last_dsp": True,
            })
        else:
            blockers.append({
                "type": "assignment",
                "id": str(a.uuid),
                "label": f"{a.group_home.name} ({a.role_type})",
            })

    for inc in Incident.objects.filter(
        reported_by=user, status__in=NON_TERMINAL_STATUSES, deleted_at__isnull=True,
    ):
        blockers.append({
            "type": "open_incident",
            "id": str(inc.uuid),
            "label": f"Reporter on incident {inc.incident_datetime or inc.created_at.date()}",
        })
    for inc in Incident.objects.filter(
        assigned_program_manager=user, status__in=NON_TERMINAL_STATUSES, deleted_at__isnull=True,
    ):
        blockers.append({
            "type": "open_incident",
            "id": str(inc.uuid),
            "label": f"Assigned PM on incident {inc.incident_datetime or inc.created_at.date()}",
        })

    return not blockers, blockers
