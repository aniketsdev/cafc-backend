from django.db import migrations


def forwards(apps, schema_editor):
    """
    For each Incident with NULL group_home, look up resident → Lead → ACTIVE
    LeadGroupHomeAssignment (the most recent one) and copy its group_home_id.
    Logs orphans (incidents whose resident has no active lead assignment).
    """
    Incident = apps.get_model("incidents", "Incident")
    LeadGroupHomeAssignment = apps.get_model("leads", "LeadGroupHomeAssignment")
    Lead = apps.get_model("leads", "Lead")

    set_count = 0
    orphan_count = 0
    for incident in Incident.objects.filter(group_home__isnull=True, deleted_at__isnull=True):
        if not incident.resident_id:
            orphan_count += 1
            continue
        lead = Lead.objects.filter(user_id=incident.resident_id).first()
        if not lead:
            orphan_count += 1
            continue
        active = (
            LeadGroupHomeAssignment.objects
            .filter(lead=lead, status="ACTIVE")
            .order_by("-created_at")
            .first()
        )
        if not active:
            orphan_count += 1
            continue
        incident.group_home_id = active.group_home_id
        incident.save(update_fields=["group_home"])
        set_count += 1

    print(f"[backfill_incident_group_home] set={set_count} orphan={orphan_count}")


def reverse(apps, schema_editor):
    # No-op: backfill is one-way. Manual cleanup if needed.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("incidents", "0013_incident_workflow_fields"),
        ("leads", "0005_leadgrouphomeassignment_unique_active_assignment_per_room"),
    ]
    operations = [
        migrations.RunPython(forwards, reverse),
    ]
