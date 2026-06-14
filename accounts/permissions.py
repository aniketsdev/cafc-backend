"""
Custom DRF permission classes for role-based access control.

Usage in views:
    from accounts.permissions import HasPermission

    class MyView(APIView):
        permission_classes = [IsAuthenticated, HasPermission]
        required_permission = "documents.view"  # module.key

        def get(self, request):
            ...

For scoped permissions (ASSIGNED_HOME), the view should also filter querysets
using `get_scoped_queryset()` utility.
"""

from rest_framework.permissions import BasePermission
from accounts.models import PermissionRoles
from group_home.models import GroupHomeStaffAssignment


# Portal (non-staff) role types. Their data access is enforced by
# application-level queryset scoping (scope_queryset_portal), NOT by the
# PermissionRoles table — see HasPermission.
PORTAL_ROLE_TYPES = {"AGENT", "GUARDIAN", "RESIDENT", "LEAD"}


def get_user_permissions(user):
    """
    Get all permissions for a user's role as a dict keyed by 'module.key'.
    Returns: {
        "documents.view": {"scope": "ALL"},
        "incidents.create": {"scope": "ASSIGNED_HOME"},
        ...
    }
    Cached on the user instance for the duration of the request.
    """
    if hasattr(user, "_cached_permissions"):
        return user._cached_permissions

    if not hasattr(user, "role") or not user.role:
        user._cached_permissions = {}
        return user._cached_permissions

    mappings = (
        PermissionRoles.objects.filter(role=user.role)
        .select_related("permission")
        .values_list("permission__module", "permission__key", "scope")
    )

    perms = {}
    for module, key, scope in mappings:
        perms[f"{module}.{key}"] = {"scope": scope}

    user._cached_permissions = perms
    return perms


def has_permission(user, permission_key):
    """
    Check if user has a specific permission.
    Returns the permission entry dict (with scope) or None.
    """
    perms = get_user_permissions(user)
    return perms.get(permission_key)


def get_permission_scope(user, permission_key):
    """
    Get the scope for a permission. Returns 'ALL', 'ASSIGNED_HOME', or None.
    """
    entry = has_permission(user, permission_key)
    return entry["scope"] if entry else None


def is_scoped_to_home(user, permission_key):
    """
    Return True if the user's permission scope is ASSIGNED_HOME.
    Returns False for ALL scope or if user has no such permission.
    """
    scope = get_permission_scope(user, permission_key)
    return scope == "ASSIGNED_HOME"


def get_effective_scope(user, permission_keys):
    """
    Given a list of permission keys, return the user's effective scope.

    Rules:
        - If user has ANY key with scope=ALL → return 'ALL' (most permissive)
        - If user has ANY key with scope=ASSIGNED_HOME → return 'ASSIGNED_HOME'
        - If user has NONE of the keys → return None

    Accepts a single string or a list of strings.
    """
    if isinstance(permission_keys, str):
        return get_permission_scope(user, permission_keys)

    found_assigned = False
    for key in permission_keys:
        scope = get_permission_scope(user, key)
        if scope == "ALL":
            return "ALL"
        if scope == "ASSIGNED_HOME":
            found_assigned = True
    return "ASSIGNED_HOME" if found_assigned else None


def scope_queryset(user, queryset, permission_key, group_home_path="group_home",
                   assignment_status_path=None,
                   active_statuses=("ASSIGNED", "ACTIVE")):
    """
    Filter a queryset based on the user's permission scope.

    If user has scope=ALL  → return queryset unchanged.
    If user has scope=ASSIGNED_HOME → filter to user.group_home only.

    Args:
        user:             The authenticated user.
        queryset:         The base queryset.
        permission_key:   A single key (str) or a list of keys.
                          e.g. "group_homes.view" or
                          ["adls.view", "goals.view", "documents.view"].
        group_home_path:  ORM path to the group_home FK on the model,
                          e.g. "group_home" or
                          "lead__group_home_assignments__group_home".
        assignment_status_path: optional ORM path to a LeadGroupHomeAssignment
                          status field that sits on the SAME relation as
                          group_home_path. When given, an ASSIGNED_HOME user
                          only matches rows whose assignment to their home is in
                          `active_statuses` — so residents who have MOVED_OUT of
                          the home drop out entirely (they no longer "belong" to
                          that home). ALL-scope roles bypass this and still see
                          moved-out residents for oversight/history.
                          e.g. "group_home_assignments__status" (Lead model) or
                          "status" (LeadGroupHomeAssignment model itself).
                          Leave None for models without an assignment status
                          (Incident, GroupHome, …) — behaviour is unchanged.
        active_statuses:  statuses considered "current" when
                          assignment_status_path is set. Defaults to
                          ASSIGNED + ACTIVE (i.e. everything except MOVED_OUT).

    Returns:
        Filtered queryset.
    """
    scope = get_effective_scope(user, permission_key)

    if scope != "ASSIGNED_HOME":
        return queryset          # ALL or no permission → view handles access

    # Union GroupHomeStaffAssignment (role-aware assignments) with the
    # user.group_homes M2M (set via user editor). Both are valid assignment
    # paths and must be combined so users see all their linked homes.
    staff_ids = set(
        GroupHomeStaffAssignment.objects
        .filter(user=user, status="ACTIVE")
        .values_list("group_home_id", flat=True)
    )
    m2m_ids = set(
        user.group_homes.values_list("id", flat=True)
    ) if hasattr(user, "group_homes") else set()
    user_home_ids = list(staff_ids | m2m_ids)

    # Legacy single-FK fallback when neither source has data
    if not user_home_ids and getattr(user, "group_home_id", None):
        user_home_ids = [user.group_home_id]

    if not user_home_ids:
        return queryset.none()   # scoped but no home assigned → empty

    # Both the home and status conditions go in ONE filter() call so they apply
    # to the SAME assignment row — a resident shows only if they have an
    # ASSIGNED/ACTIVE assignment to one of the user's homes. A lead with only a
    # MOVED_OUT assignment to the home (or who moved on to another home) no
    # longer matches and is hidden.
    filters = {f"{group_home_path}__in": user_home_ids}
    if assignment_status_path:
        filters[f"{assignment_status_path}__in"] = list(active_statuses)

    return queryset.filter(**filters).distinct()


def get_user_home_ids(user):
    """
    Return the list of group_home ids a staff user is linked to, unioned across
    ALL assignment paths so no path is missed:
      - GroupHomeStaffAssignment (role-aware assignments, status=ACTIVE)
      - user.group_homes M2M (set via the user editor)
      - legacy user.group_home single FK (fallback when the others are empty)

    Mirrors the home-resolution logic inside scope_queryset()/check_scope_access().
    A user provisioned via only one of these paths must still resolve correctly,
    otherwise scoped list/edit endpoints return empty or 422.
    """
    staff_ids = set(
        GroupHomeStaffAssignment.objects
        .filter(user=user, status="ACTIVE")
        .values_list("group_home_id", flat=True)
    )
    m2m_ids = set(
        user.group_homes.values_list("id", flat=True)
    ) if hasattr(user, "group_homes") else set()
    home_ids = list(staff_ids | m2m_ids)
    if not home_ids and getattr(user, "group_home_id", None):
        home_ids = [user.group_home_id]
    return home_ids


def scope_queryset_portal(user, queryset, guardian_path, agent_path,
                          active_assignment_path=None):
    """
    Restrict a queryset to the records a GUARDIAN/AGENT portal user is allowed
    to see, derived from the AUTHENTICATED user — never from client-supplied
    role/uuid query params.

    Portal roles bypass the PermissionRoles table (see HasPermission), and the
    staff helper scope_queryset() returns their querysets unchanged. Without
    this helper a portal user who omits the optional role/uuid params receives
    an unfiltered (all-resident) result set — a PHI cross-tenant leak.

    Behaviour:
        GUARDIAN        → records whose linked Lead.guardian is this user.
        AGENT           → records whose linked Lead.agent is this user.
        RESIDENT / LEAD → none() (no self-service list for these resources;
                          deny by default rather than leak).
        Staff / Admin   → queryset unchanged (handled by scope_queryset / view).

    Args:
        guardian_path: ORM lookup from the model to Lead.guardian's id, e.g.
                       "resident__leads__guardian_id" (Incident.resident is a
                       User), "lead__guardian_id" (Appointment.lead is a Lead),
                       "resident__guardian_id" (ConsentForm.resident is a Lead).
        agent_path:    matching ORM lookup to Lead.agent's id.
        active_assignment_path: optional lookup to a LeadGroupHomeAssignment
                       status field; when given, also restricts to "ACTIVE"
                       assignments so moved-out residents drop out of portals.
    """
    role_type = (getattr(getattr(user, "role", None), "type", "") or "").upper()

    if role_type not in PORTAL_ROLE_TYPES:
        return queryset  # staff/admin — scope handled by scope_queryset / view

    if role_type == "GUARDIAN":
        qs = queryset.filter(**{guardian_path: user.id})
    elif role_type == "AGENT":
        qs = queryset.filter(**{agent_path: user.id})
    else:
        # RESIDENT / LEAD portal logins have no list view of these resources.
        return queryset.none()

    if active_assignment_path:
        qs = qs.filter(**{active_assignment_path: "ACTIVE"})
    return qs.distinct()


def check_scope_access(user, obj, permission_key, group_home_path="group_home",
                       assignment_status_path=None,
                       active_statuses=("ASSIGNED", "ACTIVE")):
    """
    Check if a user can access a single object based on their permission scope.

    Returns True if the user has ALL scope or if the object belongs to the
    user's assigned group home.  Returns False otherwise.

    Args:
        user:             The authenticated user.
        obj:              The model instance to check.
        permission_key:   e.g. "group_homes.view", "incidents.view".
        group_home_path:  Dot-separated path to the group_home FK on the
                          model, e.g. "group_home" or
                          "resident.leads.first.group_home_assignments".
                          For GroupHome itself, pass "__self__".
        assignment_status_path / active_statuses: see scope_queryset(). When
                          set, a MOVED_OUT resident is treated as no longer in
                          the home, so an ASSIGNED_HOME user is denied access to
                          the detail too — keeping detail-by-uuid consistent with
                          the scoped list.
    """
    scope = get_permission_scope(user, permission_key)

    if scope != "ASSIGNED_HOME":
        return True  # ALL scope or no permission → view handles access

    if group_home_path == "__self__":
        # Union both assignment sources — same logic as scope_queryset.
        staff_ids = set(
            GroupHomeStaffAssignment.objects
            .filter(user=user, status="ACTIVE")
            .values_list("group_home_id", flat=True)
        )
        m2m_ids = set(
            user.group_homes.values_list("id", flat=True)
        ) if hasattr(user, "group_homes") else set()
        user_home_ids = list(staff_ids | m2m_ids)
        if not user_home_ids and getattr(user, "group_home_id", None):
            user_home_ids = [user.group_home_id]
        return obj.pk in user_home_ids

    # Use the queryset approach for complex relationships
    model_class = type(obj)
    qs = model_class.objects.filter(pk=obj.pk)
    filtered = scope_queryset(
        user, qs, permission_key, group_home_path,
        assignment_status_path=assignment_status_path,
        active_statuses=active_statuses,
    )
    return filtered.exists()


class HasPermission(BasePermission):
    """
    DRF permission class that checks the view's `required_permission` attribute
    against the user's role-based permissions.

    Usage:
        class MyView(APIView):
            permission_classes = [IsAuthenticated, HasPermission]
            required_permission = "documents.view"
    """

    message = "You do not have permission to perform this action."

    # Portal role types that bypass the PermissionRoles check.
    # Their access is governed by application-level filtering
    # (e.g. role=GUARDIAN&uuid=...), not by the staff permissions table.
    PORTAL_ROLE_TYPES = {"AGENT", "GUARDIAN", "RESIDENT", "LEAD"}

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Allow portal (non-staff) users through only if the view explicitly
        # opts in via allow_portal_roles.
        #   True          → allow ALL methods
        #   {"GET"}       → allow GET only (read-only portal access)
        #   False/absent  → block portal roles entirely
        user_role_type = getattr(
            getattr(request.user, "role", None), "type", None
        )

        # --------------------------------------------------------------------
        # ADMIN role type: unconditional full access — no DB lookup needed.
        # Admin is a superuser and should never be blocked by permission checks.
        # This also means Admin works correctly even if the seed hasn't been run.
        # --------------------------------------------------------------------
        if user_role_type == "ADMIN":
            return True

        if user_role_type in self.PORTAL_ROLE_TYPES:
            portal_flag = getattr(view, "allow_portal_roles", False)
            if isinstance(portal_flag, (set, list, tuple)):
                return request.method in portal_flag
            return bool(portal_flag)


        required = getattr(view, "required_permission", None)

        # --- OR-permission check (allow_any_permissions) ---
        # If view sets allow_any_permissions, user needs at least ONE of the listed keys.
        # Example: allow_any_permissions = {"PATCH": ["appointments.edit", "appointments.mark_completed"]}
        any_required = getattr(view, "allow_any_permissions", None)
        if any_required:
            keys = any_required.get(request.method) if isinstance(any_required, dict) else any_required
            if keys:
                if isinstance(keys, str):
                    keys = [keys]
                return any(has_permission(request.user, k) is not None for k in keys)

        # --- Group-home scoped GET bypass (opt-in) ---
        # If a view sets allow_group_home_scoped = True, a GET request is
        # allowed when:
        #   1. group_home_uuid is present in query params
        #   2. It matches the requesting user's assigned group_home.uuid
        # This enables Group Home → Users tab for Nurse/DSP/BCBA without
        # granting global users.view. All other conditions still require
        # the standard required_permission.
        if (request.method == "GET" and
                getattr(view, "allow_group_home_scoped", False)):
            gh_uuid = (request.query_params.get("group_home_uuid") or "").strip().lower()
            if gh_uuid and gh_uuid not in {"null", "undefined", "none"}:
                user_gh = getattr(request.user, "group_home", None)
                if user_gh and str(getattr(user_gh, "uuid", "")).lower() == gh_uuid:
                    return True
            # group_home_uuid missing/mismatched → fall through to standard check

        if not required:
            # No required_permission defined on the view → deny access
            return False

        # Support per-method permissions: {"GET": "docs.view", "POST": "docs.create"}
        if isinstance(required, dict):
            required = required.get(request.method)
            if not required:
                # HTTP method not mapped in the dict → deny access
                return False

        return has_permission(request.user, required) is not None


class IsAdminRole(BasePermission):
    """Allow only ADMIN role type."""

    message = "Admin access required."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return getattr(request.user, "role", None) and request.user.role.type == "ADMIN"


class IsFullAccessRole(BasePermission):
    """Allow Admin, Program Director, Program Manager, BCBA (by role name)."""

    FULL_ACCESS_NAMES = {"Admin", "Program Director", "Program Manager", "BCBA"}
    message = "Full access role required."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        role = getattr(request.user, "role", None)
        return role and role.name in self.FULL_ACCESS_NAMES
