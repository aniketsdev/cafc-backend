from django.db import migrations


def forwards(apps, schema_editor):
    """
    For every active User with a non-null group_home, create one ACTIVE
    GroupHomeStaffAssignment using the user's role.name as the role_type
    (mapped to the new RoleType enum). Idempotent — uses get_or_create on
    (group_home, user, role_type).
    """
    User = apps.get_model("accounts", "User")
    GroupHomeStaffAssignment = apps.get_model("group_home", "GroupHomeStaffAssignment")

    VALID_ROLE_TYPES = {
        "DSP", "PROGRAM_MANAGER", "PROGRAM_COORDINATOR",
        "BCBA", "NURSE", "LEAD",
    }

    NAME_MAP = {
        "DSP": "DSP",
        "Program Manager": "PROGRAM_MANAGER",
        "Program Coordinator": "PROGRAM_COORDINATOR",
        "BCBA": "BCBA",
        "Nurse": "NURSE",
        "Lead": "LEAD",
    }

    created = 0
    skipped = 0
    qs = User.objects.filter(group_home__isnull=False, active=True, deleted_at__isnull=True).select_related("role")
    for user in qs:
        role_obj = user.role
        if not role_obj:
            skipped += 1
            continue
        role_type = NAME_MAP.get(role_obj.name)
        if role_type is None or role_type not in VALID_ROLE_TYPES:
            skipped += 1
            continue
        _, was_created = GroupHomeStaffAssignment.objects.get_or_create(
            group_home_id=user.group_home_id,
            user_id=user.id,
            role_type=role_type,
            defaults={"status": "ACTIVE"},
        )
        if was_created:
            created += 1

    print(f"[backfill_staff_assignments] created={created} skipped={skipped}")


def reverse(apps, schema_editor):
    # No-op: in prod we never roll back a backfill. Manual cleanup if needed.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("group_home", "0004_grouphomestaffassignment"),
        ("accounts", "0014_permissionroles_display"),
    ]
    operations = [
        migrations.RunPython(forwards, reverse),
    ]
