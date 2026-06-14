from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from accounts.models import Role
from group_home.models import GroupHome, GroupHomeStaffAssignment
from incidents.models import Incident
from leads.models import Lead, LeadGroupHomeAssignment

_User = get_user_model()


class IncidentSerializerNewFieldsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dsp_role, _ = Role.objects.get_or_create(name="DSP", defaults={"type": "STAFF"})
        self.pm_role, _ = Role.objects.get_or_create(name="Program Manager", defaults={"type": "STAFF"})
        self.resident_role, _ = Role.objects.get_or_create(name="Resident", defaults={"type": "RESIDENT"})

        self.home = GroupHome.objects.create(
            name="Z", phone="1", fax="1", email="z@z.com", no_of_rooms=2,
        )
        self.dsp = _User.objects.create_user(
            email="dsp_ser@x.com", username="dsp_ser", first_name="D", last_name="S",
            password="pw", role=self.dsp_role, group_home=self.home,
        )
        self.pm = _User.objects.create_user(
            email="pm_ser@x.com", username="pm_ser", first_name="P", last_name="M",
            password="pw", role=self.pm_role,
        )
        self.resident = _User.objects.create_user(
            email="r_ser@x.com", username="r_ser", first_name="R", last_name="R",
            password="pw", role=self.resident_role,
        )
        GroupHomeStaffAssignment.objects.create(group_home=self.home, user=self.dsp, role_type="DSP")
        GroupHomeStaffAssignment.objects.create(group_home=self.home, user=self.pm, role_type="PROGRAM_MANAGER")

        # Resident needs an ACTIVE LeadGroupHomeAssignment so the scoped
        # incidents.view filter (resident__leads__group_home_assignments__group_home)
        # matches the DSP's assigned group home.
        self.lead = Lead.objects.create(user=self.resident)
        LeadGroupHomeAssignment.objects.create(
            lead=self.lead, group_home=self.home, status="ACTIVE",
        )

        self.client.force_authenticate(self.dsp)

    def test_create_with_group_home_and_pm_uuid(self):
        r = self.client.post("/api/incidents/", {
            "resident": str(self.resident.uuid),
            "group_home_uuid": str(self.home.uuid),
            "assigned_program_manager_uuid": str(self.pm.uuid),
            "program_manager_email": "pm_ser@x.com",
            "coordinator_email": "pm_ser@x.com",
            "incident_datetime": "2026-05-01T10:00:00Z",
            "location": "Kitchen",
            "incident_description": "test",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        inc = Incident.objects.get(uuid=r.data["data"]["uuid"])
        self.assertEqual(inc.group_home_id, self.home.id)
        self.assertEqual(inc.assigned_program_manager_id, self.pm.id)
        self.assertEqual(inc.program_manager_email, "pm_ser@x.com")
        self.assertEqual(inc.coordinator_email, "pm_ser@x.com")
        self.assertEqual(inc.status, "DRAFT")

    def test_get_returns_new_fields(self):
        inc = Incident.objects.create(
            resident=self.resident, group_home=self.home,
            assigned_program_manager=self.pm, status="DRAFT",
            program_manager_email="pm_ser@x.com",
        )
        r = self.client.get(f"/api/incidents/{inc.uuid}/")
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["data"]
        self.assertEqual(data["group_home"]["uuid"], str(self.home.uuid))
        self.assertEqual(data["assigned_program_manager"]["uuid"], str(self.pm.uuid))
        self.assertEqual(data["program_manager_email"], "pm_ser@x.com")
        self.assertIn("edits", data)
        self.assertIsInstance(data["edits"], list)


from django.contrib.contenttypes.models import ContentType
from media.models import Media
from accounts.models import Permission, PermissionRoles


def _grant_permission(role, module, key, scope="ASSIGNED_HOME"):
    """Ensure (Permission, PermissionRoles) row exists so HasPermission passes."""
    perm, _ = Permission.objects.get_or_create(
        module=module, key=key, defaults={"name": f"{module}.{key}"},
    )
    PermissionRoles.objects.get_or_create(
        role=role, permission=perm, defaults={"scope": scope},
    )
    return perm


class IncidentStartTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dsp_role, _ = Role.objects.get_or_create(name="DSP", defaults={"type": "STAFF"})
        self.resident_role, _ = Role.objects.get_or_create(name="Resident", defaults={"type": "RESIDENT"})
        # Ensure the DSP role has the new transition permissions in the test DB.
        _grant_permission(self.dsp_role, "incidents", "start")
        _grant_permission(self.dsp_role, "incidents", "submit_for_review")
        self.home = GroupHome.objects.create(
            name="StartHome", phone="1", fax="1", email="sh@x.com", no_of_rooms=2,
        )
        self.dsp = _User.objects.create_user(
            email="dsp_start@x.com", username="dsp_start", first_name="D", last_name="S",
            password="pw", role=self.dsp_role, group_home=self.home,
        )
        self.resident = _User.objects.create_user(
            email="r_start@x.com", username="r_start", first_name="R", last_name="R",
            password="pw", role=self.resident_role,
        )
        GroupHomeStaffAssignment.objects.create(group_home=self.home, user=self.dsp, role_type="DSP")

        # Need a Lead + LeadGroupHomeAssignment so scope checks pass on the resident
        from leads.models import Lead, LeadGroupHomeAssignment
        self.lead = Lead.objects.create(user=self.resident)
        LeadGroupHomeAssignment.objects.create(
            lead=self.lead, group_home=self.home, status="ACTIVE",
        )

        self.sig = Media.objects.create(
            original_filename="sig.png", file_type="IMAGE", mime_type="image/png",
            file_extension="png", file_size=10, status="active",
            content_type=ContentType.objects.get(app_label="incidents", model="incident"),
            object_id=0, s3_key="test/sig.png", s3_bucket="b",
        )
        self.inc = Incident.objects.create(
            status="DRAFT",
            resident=self.resident,
            group_home=self.home,
            reporter_signature=self.sig,
            incident_datetime="2026-05-01T10:00:00Z",
            location="Living room",
            incident_description="x",
        )
        self.client.force_authenticate(self.dsp)

    def test_start_happy_path(self):
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/start/")
        self.assertEqual(r.status_code, 200, r.data)
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.status, "IN_PROGRESS")
        self.assertIsNotNone(self.inc.started_at)

    def test_start_missing_signature(self):
        self.inc.reporter_signature = None
        self.inc.save()
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/start/")
        self.assertEqual(r.status_code, 422, r.data)
        self.assertIn(
            "Reporter signature is required before starting the incident",
            r.data["errors"],
        )

    def test_start_no_dsp_assigned_to_home(self):
        GroupHomeStaffAssignment.objects.filter(group_home=self.home).update(status="INACTIVE")
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/start/")
        self.assertEqual(r.status_code, 422, r.data)
        self.assertIn(
            "At least one DSP must be assigned to this group home",
            r.data["errors"],
        )

    def test_start_wrong_status(self):
        self.inc.status = "COMPLETED"
        self.inc.save()
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/start/")
        self.assertEqual(r.status_code, 409, r.data)


class IncidentSubmitForReviewTests(IncidentStartTests):
    def setUp(self):
        super().setUp()
        self.pm_role, _ = Role.objects.get_or_create(
            name="Program Manager", defaults={"type": "STAFF"},
        )
        self.pm = _User.objects.create_user(
            email="pm_sub@x.com", username="pm_sub", first_name="P", last_name="M",
            password="pw", role=self.pm_role, group_home=self.home,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=self.pm, role_type="PROGRAM_MANAGER",
        )

    def _advance_to_in_progress(self):
        self.inc.status = "IN_PROGRESS"
        self.inc.assigned_program_manager = self.pm
        self.inc.save()

    def test_submit_happy_path(self):
        self._advance_to_in_progress()
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/submit-for-review/")
        self.assertEqual(r.status_code, 200, r.data)
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.status, "PM_REVIEW_PENDING")
        self.assertIsNotNone(self.inc.submitted_for_review_at)
        self.assertEqual(
            self.inc.incident_notifications.filter(type="PROGRAM_MANAGER", user=self.pm).count(),
            1,
        )

    def test_submit_no_pm_assigned(self):
        self._advance_to_in_progress()
        self.inc.assigned_program_manager = None
        self.inc.save()
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/submit-for-review/")
        self.assertEqual(r.status_code, 422, r.data)

    def test_submit_pm_not_assigned_to_home(self):
        self._advance_to_in_progress()
        GroupHomeStaffAssignment.objects.filter(user=self.pm).update(status="INACTIVE")
        r = self.client.post(f"/api/incidents/{self.inc.uuid}/submit-for-review/")
        self.assertEqual(r.status_code, 422, r.data)


class IncidentSendBackTests(IncidentSubmitForReviewTests):
    def setUp(self):
        super().setUp()
        # Move to PM_REVIEW_PENDING, authenticate as the PM
        _grant_permission(self.pm_role, "incidents", "pm_review")

    def _advance_to_pm_review(self):
        """Move the inherited incident to PM_REVIEW_PENDING and authenticate as PM."""
        self.inc.status = "PM_REVIEW_PENDING"
        self.inc.assigned_program_manager = self.pm
        self.inc.save()
        self.client.force_authenticate(self.pm)

    def test_send_back_happy_path(self):
        self._advance_to_pm_review()
        r = self.client.post(
            f"/api/incidents/{self.inc.uuid}/send-back/",
            {"reason": "Please clarify the response action"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.status, "IN_PROGRESS")
        self.assertEqual(self.inc.comments.count(), 1)

    def test_send_back_short_reason(self):
        self._advance_to_pm_review()
        r = self.client.post(
            f"/api/incidents/{self.inc.uuid}/send-back/",
            {"reason": "no"},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)

    def test_send_back_not_the_assigned_pm(self):
        self._advance_to_pm_review()
        # Authenticate as a different PM, who is also assigned to the home (so permission passes)
        other_pm = _User.objects.create_user(
            email="otherpm@x.com", username="otherpm", first_name="O", last_name="P",
            password="pw", role=self.pm_role, group_home=self.home,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=other_pm, role_type="PROGRAM_MANAGER",
        )
        self.client.force_authenticate(other_pm)
        r = self.client.post(
            f"/api/incidents/{self.inc.uuid}/send-back/",
            {"reason": "trying to interfere"}, format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)


class IncidentPMSignoffTests(IncidentSendBackTests):
    def setUp(self):
        super().setUp()
        # Add a Lead with guardian + agent so notifications can be created
        self.guardian_role, _ = Role.objects.get_or_create(
            name="Guardian", defaults={"type": "GUARDIAN"},
        )
        self.agent_role, _ = Role.objects.get_or_create(
            name="Agent", defaults={"type": "AGENT"},
        )
        self.guardian = _User.objects.create_user(
            email="g_signoff@x.com", username="g_signoff", first_name="G", last_name="G",
            password="pw", role=self.guardian_role,
        )
        self.agent = _User.objects.create_user(
            email="a_signoff@x.com", username="a_signoff", first_name="A", last_name="A",
            password="pw", role=self.agent_role,
        )
        # Update existing Lead (from IncidentStartTests.setUp)
        self.lead.guardian = self.guardian
        self.lead.agent = self.agent
        self.lead.save()

        # PM signature media (must reference this incident.id so the view's check passes)
        self.pm_sig = Media.objects.create(
            original_filename="pmsig.png", file_type="IMAGE", mime_type="image/png",
            file_extension="png", file_size=10, status="active",
            content_type=ContentType.objects.get(app_label="incidents", model="incident"),
            object_id=self.inc.id, s3_key="test/pmsig.png", s3_bucket="b",
        )

    def _advance_to_pm_review_for_signoff(self):
        """Move the inherited incident to PM_REVIEW_PENDING and authenticate as PM."""
        self.inc.status = "PM_REVIEW_PENDING"
        self.inc.assigned_program_manager = self.pm
        self.inc.save()
        self.client.force_authenticate(self.pm)

    def test_signoff_happy_path(self):
        self._advance_to_pm_review_for_signoff()
        r = self.client.post(
            f"/api/incidents/{self.inc.uuid}/pm-signoff/",
            {"pm_signature_media_id": str(self.pm_sig.id)}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.status, "COMPLETED")
        self.assertEqual(self.inc.pm_signature_id, self.pm_sig.id)
        pm_full_name = f"{self.pm.first_name or ''} {self.pm.last_name or ''}".strip() or self.pm.email
        types = set(self.inc.incident_notifications.filter(by_whom=pm_full_name).values_list("type", flat=True))
        self.assertEqual(types, {"GUARDIAN", "AGENT"})

    def test_signoff_idempotent(self):
        self._advance_to_pm_review_for_signoff()
        self.client.post(
            f"/api/incidents/{self.inc.uuid}/pm-signoff/",
            {"pm_signature_media_id": str(self.pm_sig.id)}, format="json",
        )
        self.inc.refresh_from_db()
        first_completed_at = self.inc.completed_at
        r = self.client.post(
            f"/api/incidents/{self.inc.uuid}/pm-signoff/",
            {"pm_signature_media_id": str(self.pm_sig.id)}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.completed_at, first_completed_at)
        pm_full_name = f"{self.pm.first_name or ''} {self.pm.last_name or ''}".strip() or self.pm.email
        self.assertEqual(
            self.inc.incident_notifications.filter(by_whom=pm_full_name, type="GUARDIAN").count(),
            1,
        )

    def test_signoff_wrong_pm(self):
        self._advance_to_pm_review_for_signoff()
        other_pm = _User.objects.create_user(
            email="otherpm2@x.com", username="otherpm2", first_name="O", last_name="2",
            password="pw", role=self.pm_role, group_home=self.home,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=other_pm, role_type="PROGRAM_MANAGER",
        )
        self.client.force_authenticate(other_pm)
        r = self.client.post(
            f"/api/incidents/{self.inc.uuid}/pm-signoff/",
            {"pm_signature_media_id": str(self.pm_sig.id)}, format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)
