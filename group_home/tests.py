from django.test import TestCase
from django.db import IntegrityError
from django.contrib.auth import get_user_model

from accounts.models import Role
from group_home.models import GroupHome, GroupHomeStaffAssignment


User = get_user_model()


class GroupHomeStaffAssignmentTests(TestCase):
    def setUp(self):
        self.role, _ = Role.objects.get_or_create(
            name="DSP", defaults={"type": "STAFF"},
        )
        self.home = GroupHome.objects.create(
            name="Home A", phone="555", fax="555", email="a@x.com", no_of_rooms=4,
        )
        self.user = User.objects.create_user(
            email="dsp@x.com", username="dsp", first_name="D", last_name="SP",
            password="pw", role=self.role,
        )

    def test_create_assignment(self):
        a = GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=self.user, role_type="DSP",
        )
        self.assertEqual(a.status, "ACTIVE")
        self.assertIsNotNone(a.uuid)

    def test_unique_together(self):
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=self.user, role_type="DSP",
        )
        with self.assertRaises(IntegrityError):
            GroupHomeStaffAssignment.objects.create(
                group_home=self.home, user=self.user, role_type="DSP",
            )

    def test_same_user_two_roles_allowed(self):
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=self.user, role_type="DSP",
        )
        a2 = GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=self.user, role_type="PROGRAM_MANAGER",
        )
        self.assertEqual(a2.role_type, "PROGRAM_MANAGER")


from rest_framework.test import APIClient
from rest_framework import status as http_status


class AssignmentAPIBaseTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin_role, _ = Role.objects.get_or_create(name="Admin", defaults={"type": "ADMIN"})
        self.dsp_role, _ = Role.objects.get_or_create(name="DSP", defaults={"type": "STAFF"})
        self.home = GroupHome.objects.create(name="A2", phone="1", fax="1", email="a2@a.com", no_of_rooms=2)
        self.admin = User.objects.create_user(
            email="admin2@x.com", username="admin2", first_name="A", last_name="A", password="pw",
            role=self.admin_role, is_superuser=True,
        )
        self.dsp = User.objects.create_user(
            email="dsp1@x.com", username="dsp1", first_name="D", last_name="One", password="pw",
            role=self.dsp_role,
        )
        GroupHomeStaffAssignment.objects.create(group_home=self.home, user=self.dsp, role_type="DSP")
        self.client.force_authenticate(self.admin)


class AssignmentListTests(AssignmentAPIBaseTests):
    def test_list_assignments(self):
        url = f"/api/group-homes/{self.home.uuid}/assignments/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, http_status.HTTP_200_OK)
        self.assertEqual(len(r.data["data"]), 1)
        self.assertEqual(r.data["data"][0]["role_type"], "DSP")

    def test_filter_by_role(self):
        url = f"/api/group-homes/{self.home.uuid}/assignments/?role=PROGRAM_MANAGER"
        r = self.client.get(url)
        self.assertEqual(r.status_code, http_status.HTTP_200_OK)
        self.assertEqual(len(r.data["data"]), 0)


class AssignmentCreateTests(AssignmentAPIBaseTests):
    def test_create_new_assignment(self):
        new_dsp = User.objects.create_user(
            email="dsp2@x.com", username="dsp2", first_name="D", last_name="Two",
            password="pw", role=self.dsp_role,
        )
        url = f"/api/group-homes/{self.home.uuid}/assignments/"
        r = self.client.post(url, {"user_uuid": new_dsp.uuid, "role_type": "DSP"}, format="json")
        self.assertEqual(r.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(GroupHomeStaffAssignment.objects.filter(user=new_dsp, status="ACTIVE").count(), 1)

    def test_reactivate_inactive_assignment(self):
        a = GroupHomeStaffAssignment.objects.get(user=self.dsp, group_home=self.home, role_type="DSP")
        a.status = "INACTIVE"; a.save()
        url = f"/api/group-homes/{self.home.uuid}/assignments/"
        r = self.client.post(url, {"user_uuid": self.dsp.uuid, "role_type": "DSP"}, format="json")
        self.assertEqual(r.status_code, http_status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.status, "ACTIVE")

    def test_invalid_role_type(self):
        url = f"/api/group-homes/{self.home.uuid}/assignments/"
        r = self.client.post(url, {"user_uuid": self.dsp.uuid, "role_type": "PLUMBER"}, format="json")
        self.assertEqual(r.status_code, 400)


class AssignmentDeleteTests(AssignmentAPIBaseTests):
    def test_soft_delete(self):
        a = GroupHomeStaffAssignment.objects.get(user=self.dsp, group_home=self.home)
        url = f"/api/group-homes/{self.home.uuid}/assignments/{a.uuid}/"
        r = self.client.delete(url)
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.status, "INACTIVE")
        self.assertIsNotNone(a.deleted_at)

    def test_delete_not_found(self):
        import uuid as u
        url = f"/api/group-homes/{self.home.uuid}/assignments/{u.uuid4()}/"
        r = self.client.delete(url)
        self.assertEqual(r.status_code, 404)


class GroupHomeCanDeleteTests(AssignmentAPIBaseTests):
    def test_can_delete_blocked_by_assignment(self):
        url = f"/api/group-homes/{self.home.uuid}/can-delete/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["can_delete"])
        self.assertTrue(any(b["type"] == "assignment" for b in r.data["blockers"]))

    def test_can_delete_after_unassign(self):
        self.home.staff_assignments.update(status="INACTIVE")
        url = f"/api/group-homes/{self.home.uuid}/can-delete/"
        r = self.client.get(url)
        self.assertTrue(r.data["can_delete"])
        self.assertEqual(r.data["blockers"], [])

    def test_can_delete_blocked_by_open_incident(self):
        # Remove assignments so only the open incident is the blocker
        self.home.staff_assignments.update(status="INACTIVE")
        from incidents.models import Incident
        Incident.objects.create(group_home=self.home, status="DRAFT")
        url = f"/api/group-homes/{self.home.uuid}/can-delete/"
        r = self.client.get(url)
        self.assertFalse(r.data["can_delete"])
        self.assertTrue(any(b["type"] == "open_incident" for b in r.data["blockers"]))


class GroupHomeDeleteGuardTests(AssignmentAPIBaseTests):
    def test_delete_blocked_when_active_assignment(self):
        # The setUp already creates an active assignment
        r = self.client.delete(f"/api/group-homes/{self.home.uuid}/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("blockers", r.data)

    def test_delete_succeeds_after_unassign(self):
        self.home.staff_assignments.update(status="INACTIVE")
        r = self.client.delete(f"/api/group-homes/{self.home.uuid}/")
        # Depending on existing view: 200 OK or 204 No Content; both are fine
        self.assertIn(r.status_code, (200, 204))
