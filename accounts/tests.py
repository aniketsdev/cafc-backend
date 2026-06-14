from django.test import TestCase
from django.contrib.auth import get_user_model

from accounts.models import Permission, PermissionRoles, Role
from accounts.permissions import scope_queryset
from group_home.models import GroupHome, GroupHomeStaffAssignment
from incidents.models import Incident

User = get_user_model()


class ScopeQuerysetTests(TestCase):
    def setUp(self):
        # Roles may already exist from seed_default_roles data migration
        self.dsp_role, _ = Role.objects.get_or_create(name="DSP", defaults={"type": "STAFF"})

        # Ensure DSP has incidents.view permission scoped to ASSIGNED_HOME
        view_perm, _ = Permission.objects.get_or_create(
            module="incidents", key="view", defaults={"name": "View Incidents"}
        )
        PermissionRoles.objects.get_or_create(
            role=self.dsp_role,
            permission=view_perm,
            defaults={"scope": "ASSIGNED_HOME"},
        )

        self.home_a = GroupHome.objects.create(
            name="A", phone="1", fax="1", email="a@a.com", no_of_rooms=2,
        )
        self.home_b = GroupHome.objects.create(
            name="B", phone="2", fax="2", email="b@b.com", no_of_rooms=2,
        )
        self.user = User.objects.create_user(
            email="x@x.com", username="x", first_name="X", last_name="X",
            password="pwwytyw", role=self.dsp_role,
        )
        # ONLY assigned to home_a
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home_a, user=self.user, role_type="DSP",
        )
        self.inc_a = Incident.objects.create(
            group_home=self.home_a, status="DRAFT",
        )
        self.inc_b = Incident.objects.create(
            group_home=self.home_b, status="DRAFT",
        )

    def test_assigned_home_scope_filters_to_assigned(self):
        qs = Incident.objects.all()
        scoped = scope_queryset(
            self.user, qs, "incidents.view",
            group_home_path="group_home",
        )
        ids = set(scoped.values_list("id", flat=True))
        self.assertIn(self.inc_a.id, ids)
        self.assertNotIn(self.inc_b.id, ids)


from leads.models import Lead, LeadGroupHomeAssignment


class MovedOutScopeTests(TestCase):
    """
    ASSIGNED_HOME roles (Coordinator/DSP/Nurse) must NOT see residents who have
    MOVED_OUT of their home — once a resident leaves, they no longer belong to
    that home. ALL-scope roles (Admin/PM) still see moved-out for oversight.
    Guards scope_queryset(..., assignment_status_path=...).
    """

    def setUp(self):
        self.coord_role, _ = Role.objects.get_or_create(
            name="Program Coordinator", defaults={"type": "STAFF"}
        )
        self.admin_role, _ = Role.objects.get_or_create(
            name="Admin", defaults={"type": "ADMIN"}
        )
        for role, scope in ((self.coord_role, "ASSIGNED_HOME"), (self.admin_role, "ALL")):
            perm, _ = Permission.objects.get_or_create(
                module="leads", key="view", defaults={"name": "leads.view"}
            )
            PermissionRoles.objects.update_or_create(
                role=role, permission=perm, defaults={"scope": scope},
            )

        self.home_a = GroupHome.objects.create(
            name="MA", phone="1", fax="1", email="ma@x.com", no_of_rooms=2,
        )
        self.home_b = GroupHome.objects.create(
            name="MB", phone="2", fax="2", email="mb@x.com", no_of_rooms=2,
        )
        self.coord = User.objects.create_user(
            email="mo_coord@x.com", username="mo_coord", first_name="C", last_name="O",
            password="pw", role=self.coord_role,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home_a, user=self.coord, role_type="PROGRAM_COORDINATOR",
        )
        self.admin = User.objects.create_user(
            email="mo_admin@x.com", username="mo_admin", first_name="A", last_name="D",
            password="pw", role=self.admin_role,
        )

        self.active = self._lead("mo_active", self.home_a, "ACTIVE")
        self.moved_out = self._lead("mo_out", self.home_a, "MOVED_OUT")

    def _lead(self, slug, home, assignment_status):
        u = User.objects.create_user(
            email=f"{slug}@x.com", username=slug, first_name="L", last_name="D",
            password="pw", role=self.coord_role,
        )
        lead = Lead.objects.create(user=u, status="DRAFT")
        LeadGroupHomeAssignment.objects.create(
            lead=lead, group_home=home, status=assignment_status,
        )
        return lead

    def _scoped_ids(self, user):
        qs = scope_queryset(
            user, Lead.objects.all(), "leads.view",
            group_home_path="group_home_assignments__group_home",
            assignment_status_path="group_home_assignments__status",
        )
        return set(qs.values_list("id", flat=True))

    def test_assigned_home_hides_moved_out(self):
        ids = self._scoped_ids(self.coord)
        self.assertIn(self.active.id, ids)
        self.assertNotIn(self.moved_out.id, ids)

    def test_all_scope_still_sees_moved_out(self):
        ids = self._scoped_ids(self.admin)
        self.assertIn(self.active.id, ids)
        self.assertIn(self.moved_out.id, ids)


from rest_framework.test import APIClient


class UserCanDeleteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin_role, _ = Role.objects.get_or_create(name="Admin", defaults={"type": "ADMIN"})
        self.dsp_role, _ = Role.objects.get_or_create(name="DSP", defaults={"type": "STAFF"})
        self.home = GroupHome.objects.create(name="C", phone="1", fax="1", email="c@c.com", no_of_rooms=2)
        self.admin = User.objects.create_user(
            email="admin3@x.com", username="admin3", first_name="A", last_name="A",
            password="pw", role=self.admin_role, is_superuser=True,
        )
        self.dsp = User.objects.create_user(
            email="dsp3@x.com", username="dsp3", first_name="D", last_name="S",
            password="pw", role=self.dsp_role,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home, user=self.dsp, role_type="DSP",
        )
        self.client.force_authenticate(self.admin)

    def test_blocked_by_assignment(self):
        r = self.client.get(f"/api/accounts/users/{self.dsp.uuid}/can-delete/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["can_delete"])

    def test_clean_user_can_delete(self):
        clean = User.objects.create_user(
            email="clean@x.com", username="clean", first_name="C", last_name="L",
            password="pw", role=self.dsp_role,
        )
        r = self.client.get(f"/api/accounts/users/{clean.uuid}/can-delete/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["can_delete"])


from leads.models import Lead, LeadGroupHomeAssignment


class CoordinatorListScopeRegressionTests(TestCase):
    """
    RBAC regression guard — see QA Retest 2026-06-02.

    A Program Coordinator (STAFF, scope=ASSIGNED_HOME on users.view / leads.view)
    must NOT see the org-wide list returned to Admin (scope=ALL). The bug was a
    Coordinator getting total_records identical to Admin on GET /api/accounts/users/
    and GET /api/leads/ — the entire org, every group home. These tests assert the
    Coordinator's total_records is STRICTLY LESS than Admin's (equality = leak), and
    pins the exact home-scoped count so a future seed/view change can't silently
    re-widen the scope.
    """

    def setUp(self):
        self.client = APIClient()

        self.admin_role, _ = Role.objects.get_or_create(
            name="Admin", defaults={"type": "ADMIN"}
        )
        self.coord_role, _ = Role.objects.get_or_create(
            name="Program Coordinator", defaults={"type": "STAFF"}
        )

        # Permissions + role mappings, mirroring the FIXED seed:
        #   Admin       → scope ALL          (org-wide)
        #   Coordinator → scope ASSIGNED_HOME (own home only)
        for module, key in (("users", "view"), ("leads", "view")):
            perm, _ = Permission.objects.get_or_create(
                module=module, key=key, defaults={"name": f"{module}.{key}"}
            )
            PermissionRoles.objects.update_or_create(
                role=self.admin_role, permission=perm, defaults={"scope": "ALL"},
            )
            PermissionRoles.objects.update_or_create(
                role=self.coord_role, permission=perm,
                defaults={"scope": "ASSIGNED_HOME"},
            )

        self.home_a = GroupHome.objects.create(
            name="HA", phone="1", fax="1", email="ha@x.com", no_of_rooms=2,
        )
        self.home_b = GroupHome.objects.create(
            name="HB", phone="2", fax="2", email="hb@x.com", no_of_rooms=2,
        )

        # Admin (no home assignment needed — scope=ALL).
        self.admin = User.objects.create_user(
            email="admin_rbac@x.com", username="admin_rbac",
            first_name="Ad", last_name="Min", password="pw", role=self.admin_role,
        )
        # Coordinator assigned ONLY to home_a.
        self.coord = User.objects.create_user(
            email="coord_rbac@x.com", username="coord_rbac",
            first_name="Co", last_name="Ord", password="pw", role=self.coord_role,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=self.home_a, user=self.coord, role_type="PROGRAM_COORDINATOR",
        )

        # Other staff: one in the coordinator's home, two outside it.
        self._make_user_in_home("in_a@x.com", self.home_a)
        self._make_user_in_home("out_b1@x.com", self.home_b)
        self._make_user_in_home("out_b2@x.com", self.home_b)

        # Leads: one assigned to home_a (Coordinator-visible), two to home_b.
        self.lead_a = self._make_lead_in_home("lead_a", self.home_a)
        self._make_lead_in_home("lead_b1", self.home_b)
        self._make_lead_in_home("lead_b2", self.home_b)

    def _make_user_in_home(self, email, home):
        u = User.objects.create_user(
            email=email, username=email, first_name="U", last_name="U",
            password="pw", role=self.coord_role,
        )
        GroupHomeStaffAssignment.objects.create(
            group_home=home, user=u, role_type="DSP",
        )
        return u

    def _make_lead_in_home(self, slug, home):
        lead_user = User.objects.create_user(
            email=f"{slug}@x.com", username=slug, first_name="L", last_name="D",
            password="pw", role=self.coord_role,
        )
        lead = Lead.objects.create(user=lead_user, status="DRAFT")
        LeadGroupHomeAssignment.objects.create(
            lead=lead, group_home=home, status="ACTIVE",
        )
        return lead

    def _total(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}: {r.data}")
        return r.data["data"]["pagination"]["total_records"]

    def test_coordinator_users_list_is_scoped_below_admin(self):
        self.client.force_authenticate(self.admin)
        admin_total = self._total("/api/accounts/users/?page=1")

        self.client.force_authenticate(self.coord)
        coord_total = self._total("/api/accounts/users/?page=1")

        # The regression guard: Coordinator must NOT match Admin's org-wide count.
        self.assertLess(
            coord_total, admin_total,
            "Coordinator sees the same user count as Admin — RBAC leak regressed",
        )
        # Pin the exact scope: only the 2 users linked to home_a
        # (the Coordinator + the one DSP assigned there).
        self.assertEqual(coord_total, 2)

    def test_coordinator_leads_list_is_scoped_below_admin(self):
        self.client.force_authenticate(self.admin)
        admin_total = self._total("/api/leads/?page=1")

        self.client.force_authenticate(self.coord)
        coord_total = self._total("/api/leads/?page=1")

        self.assertLess(
            coord_total, admin_total,
            "Coordinator sees the same lead count as Admin — RBAC leak regressed",
        )
        # Only the single lead assigned to home_a.
        self.assertEqual(coord_total, 1)
