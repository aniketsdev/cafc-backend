from django.conf import settings
from .models import AuditLog


def log_audit(
    *,
    request=None,
    action,
    entity_type,
    entity_id=None,
    group_home=None,
    message=None,
    target_user=None,
):
    """
    Central audit logging helper.

    Args:
        target_user: Optional explicit target user (the person the action
                     was performed ON/FOR). If not provided, auto-resolved
                     from entity_type + entity_id when possible.
    """

    if not getattr(settings, "AUDIT_LOG_ENABLED", True):
        return

    user = None
    if request and hasattr(request, "user") and request.user.is_authenticated:
        user = request.user

    # ✅ DYNAMIC ENTITY MAPPING & CONTEXT ENHANCEMENT
    resolved_target = None
    mapping_targets = [
        "User", "Lead", "LeadGroupHomeAssignment",
        "CarePlanItem", "CarePlanDailyLog", "CarePlanReport",
        "Appointment", "Incident",
    ]
    if entity_type in mapping_targets and entity_id:
        from django.apps import apps

        def _resolve(model_class, eid, select_related_fields=None):
            """Try UUID lookup first, fallback to integer ID for backward compat with old audit logs."""
            qs = model_class.objects.all()
            if select_related_fields:
                qs = qs.select_related(*select_related_fields)
            # Try UUID first (new logs)
            try:
                obj = qs.filter(uuid=eid).first()
                if obj:
                    return obj
            except Exception:
                pass
            # Fallback to integer ID (old logs)
            try:
                return qs.filter(id=int(eid)).first()
            except (ValueError, TypeError):
                return None

        try:
            # Identify target user and organizational context based on entity type
            if entity_type == "User":
                UserModel = apps.get_model("accounts", "User")
                resolved_target = _resolve(UserModel, entity_id, ["role", "group_home"])

            elif entity_type == "Lead":
                LeadModel = apps.get_model("leads", "Lead")
                lead = _resolve(LeadModel, entity_id, ["user__role", "user__group_home"])
                if lead:
                    resolved_target = lead.user

            elif entity_type == "LeadGroupHomeAssignment":
                AssignmentModel = apps.get_model("leads", "LeadGroupHomeAssignment")
                assignment = _resolve(AssignmentModel, entity_id, [
                    "lead__user__role", "lead__user__group_home", "group_home"
                ])
                if assignment:
                    if assignment.lead:
                        resolved_target = assignment.lead.user
                    if not group_home:
                        group_home = assignment.group_home

            elif entity_type == "CarePlanItem":
                ItemModel = apps.get_model("residents", "CarePlanItem")
                item = _resolve(ItemModel, entity_id, ["resident__role", "resident__group_home"])
                if item:
                    resolved_target = item.resident
                entity_type = "Resident"

            elif entity_type == "CarePlanDailyLog":
                LogModel = apps.get_model("residents", "CarePlanDailyLog")
                log = _resolve(LogModel, entity_id, ["resident__role", "resident__group_home"])
                if log:
                    resolved_target = log.resident
                entity_type = "Resident"

            elif entity_type == "CarePlanReport":
                ReportModel = apps.get_model("residents", "CarePlanReport")
                report = _resolve(ReportModel, entity_id, ["resident__role", "resident__group_home"])
                if report:
                    resolved_target = report.resident
                entity_type = "Resident"

            elif entity_type == "Appointment":
                AppointmentModel = apps.get_model("appointments", "Appointment")
                appt = _resolve(AppointmentModel, entity_id, [
                    "lead__user__role", "lead__user__group_home"
                ])
                if appt and appt.lead:
                    resolved_target = appt.lead.user

            elif entity_type == "Incident":
                IncidentModel = apps.get_model("incidents", "Incident")
                incident = _resolve(IncidentModel, entity_id, [
                    "resident__role", "resident__group_home"
                ])
                if incident:
                    resolved_target = incident.resident

            if resolved_target:
                # 🏷️ Role-based reclassification for general models
                if resolved_target.role and entity_type not in ["Resident", "Incident", "Appointment"]:
                    if resolved_target.role.type == "AGENT":
                        entity_type = "Agent"
                    elif resolved_target.role.type == "RESIDENT":
                        entity_type = "Resident"

                # 👤 AUTO-LINK USER
                if not user:
                    user = resolved_target

                # 🏠 AUTO-LINK GROUP HOME
                if not group_home:
                    # First try direct group_home field
                    if resolved_target.group_home:
                        group_home = resolved_target.group_home
                    # For residents, check active assignment if no direct group_home
                    elif resolved_target.role and resolved_target.role.type == "RESIDENT":
                        try:
                            LeadGroupHomeAssignmentModel = apps.get_model("leads", "LeadGroupHomeAssignment")
                            active_assignment = LeadGroupHomeAssignmentModel.objects.select_related("group_home").filter(
                                lead__user=resolved_target,
                                status="ACTIVE"
                            ).first()
                            if active_assignment:
                                group_home = active_assignment.group_home
                        except Exception:
                            pass
        except Exception:
            pass

    # 🎯 Determine final target user: explicit param takes precedence
    final_target = target_user or resolved_target

    # 📸 Capture target user name for immutable historical record
    target_user_name_snapshot = None
    if final_target:
        first = getattr(final_target, "first_name", "") or ""
        last = getattr(final_target, "last_name", "") or ""
        target_user_name_snapshot = f"{first} {last}".strip() or None

    # 📸 Capture group home name for immutable historical record
    log_group_home_name = None
    if group_home:
        log_group_home_name = group_home.name

    AuditLog.objects.create(
        user=user,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        group_home=group_home,
        log_group_home_name=log_group_home_name,
        target_user=final_target,
        target_user_name=target_user_name_snapshot,
        message=message,
        ip_address=_get_client_ip(request),
    )


# 🔁 BACKWARD COMPATIBILITY
log_action = log_audit


def _get_client_ip(request):
    if not request:
        return None

    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR")


def get_active_group_home(*args, **kwargs):
    """
    Stub kept ONLY to avoid import crashes.
    """
    return None
