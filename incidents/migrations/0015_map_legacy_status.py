from django.db import migrations


STATUS_MAP = {
    "OPEN": "DRAFT",
    "CLOSED": "COMPLETED",
    "ACKNOWLEDGED": "ACKNOWLEDGED",
}


def forwards(apps, schema_editor):
    Incident = apps.get_model("incidents", "Incident")
    total = 0
    for old, new in STATUS_MAP.items():
        n = Incident.objects.filter(status=old).update(status=new)
        total += n
        print(f"[map_legacy_status] {old} -> {new}: {n}")
    print(f"[map_legacy_status] total updated: {total}")


def reverse(apps, schema_editor):
    Incident = apps.get_model("incidents", "Incident")
    for old, new in STATUS_MAP.items():
        # NOTE: reverse is lossy — ACKNOWLEDGED stays ACKNOWLEDGED in both directions,
        # but a DRAFT row may have originated either from OPEN or new code creating a draft.
        # Reverse only the rows we created via the map.
        Incident.objects.filter(status=new).update(status=old)


class Migration(migrations.Migration):
    dependencies = [
        ("incidents", "0014_backfill_incident_group_home"),
    ]
    operations = [
        migrations.RunPython(forwards, reverse),
    ]
