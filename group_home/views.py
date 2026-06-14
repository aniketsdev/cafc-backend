from django.utils import timezone
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from group_home.models import GroupHome, GroupHomeRoom, GroupHomeStaffAssignment
from leads.models import LeadGroupHomeAssignment
from group_home.serializers import GroupHomeSerializer, GroupHomeStaffAssignmentSerializer
from audit_logs.utils import log_action, get_active_group_home
from accounts.permissions import scope_queryset, check_scope_access, HasPermission

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from group_home.serializers import GroupHomeStatusSerializer

# For user API
from accounts.models import User, Role
from group_home.serializers import GroupHomeUserSerializer


# =====================================================
# LIST & CREATE GROUP HOME
# =====================================================
class GroupHomeListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "group_homes.view",    # All staff + ASSIGNED_HOME scope inline
        "POST": "group_homes.create",  # Admin, PD only
    }
    # G/A can view list — inline scope_queryset limits to their residents' homes
    allow_portal_roles = {"GET"}

    @extend_schema(
        operation_id="list_group_homes",
        summary="List group homes",
        description="Fetch active group homes with pagination. For GUARDIAN/AGENT roles, returns only group homes linked to residents they are associated with (guardian/agent → resident → group home). For staff, respects ASSIGNED_HOME scope.",
        tags=["Group Home"],
        parameters=[
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page number"
            ),
            OpenApiParameter(
                name="size",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page size"
            ),
        ],
        responses={200: GroupHomeSerializer(many=True)},
    )
    def get(self, request):
        # Read pagination params
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))

        # Fetch active group homes only (soft delete respected)
        queryset = (
                GroupHome.objects
                .filter(deleted_at__isnull=True)
                .prefetch_related("media_items")
                .order_by("-created_at")
            )

        # Role-based access: restrict to group homes the user is authorized to see
        if request.user.is_authenticated:
            role_type = getattr(
                getattr(request.user, "role", None), "type", None
            ) or ""

            if role_type.upper() == "GUARDIAN":
                # Only group homes where the guardian has at least one resident (lead) with an active assignment
                queryset = queryset.filter(
                    lead_assignments__lead__guardian_id=request.user.id,
                    lead_assignments__status__in=["ACTIVE", "ASSIGNED"],
                ).distinct()
            elif role_type.upper() == "AGENT":
                # Only group homes where the agent has at least one resident (lead) with an active assignment
                queryset = queryset.filter(
                    lead_assignments__lead__agent_id=request.user.id,
                    lead_assignments__status__in=["ACTIVE", "ASSIGNED"],
                ).distinct()
            else:
                # Staff/admin: scope to assigned group home for ASSIGNED_HOME users
                queryset = scope_queryset(
                    request.user, queryset, "group_homes.view", group_home_path="id"
                )

        # Apply pagination
        paginator = Paginator(queryset, size)
        paginated_data = paginator.get_page(page)

        serializer = GroupHomeSerializer(paginated_data, many=True)

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Group homes fetched successfully",
                "data": {
                    "results": serializer.data,
                    "pagination": {
                        "page": page,
                        "size": size,
                        "total_pages": paginator.num_pages,
                        "total_records": paginator.count,
                    },
                },
            }
        )

    # -------------------------------------------------
    # CREATE GROUP HOME (POST)
    # -------------------------------------------------
    # ✔ Creates GroupHome
    # ✔ Creates Address + License
    # ✔ Auto-creates rooms
    # ✔ Creates shifts
    # -------------------------------------------------
    @extend_schema(
        operation_id="create_group_home",
        summary="Create group home",
        description="Create a new group home with address, license, rooms, and shifts.",
        request=GroupHomeSerializer,
        responses={201: GroupHomeSerializer},
        tags=["Group Home"],
    )
    def post(self, request):
        serializer = GroupHomeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        group_home = serializer.save()

        log_action(
                request=request,
                action="CREATE",
                entity_type="GroupHome",
                entity_id=str(group_home.uuid),
                group_home=group_home,
                message="Group home created",
            )


        return Response(
            {
                "status": "success",
                "code": 201,
                "message": "Group home created successfully",
                "data": GroupHomeSerializer(group_home).data,
            },
            status=201,
        )


# =====================================================
# RETRIEVE / UPDATE / DELETE GROUP HOME
# =====================================================
class GroupHomeDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":    "group_homes.view_profile",  # All staff + ASSIGNED_HOME; G/A via portal
        "PUT":    "group_homes.edit",          # Admin, PD, PM, Coordinator
        "DELETE": "group_homes.delete",        # Admin only
    }
    # G/A can view a group home profile
    allow_portal_roles = {"GET"}

    # -------------------------------------------------
    # Helper: Fetch group home by UUID
    # -------------------------------------------------
    def get_object(self, uuid):
        try:
            return (
                GroupHome.objects
                .filter(uuid=uuid, deleted_at__isnull=True)
                .prefetch_related("media_items")  # ✅ REQUIRED
                .first()
            )
        except GroupHome.DoesNotExist:
            return None


    # -------------------------------------------------
    # RETRIEVE GROUP HOME (GET)
    # -------------------------------------------------
    @extend_schema(
        operation_id="retrieve_group_home",
        summary="Get group home details",
        description="Retrieve a single group home by UUID.",
        tags=["Group Home"],
        parameters=[
            OpenApiParameter(
                name="uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="Group Home UUID"
            )
        ],
        responses={200: GroupHomeSerializer},
    )
    def get(self, request, uuid):
        group_home = self.get_object(uuid)
        if not group_home:
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )

        # Scope check: ASSIGNED_HOME users can only access their own home
        if not check_scope_access(request.user, group_home, "group_homes.view", "__self__"):
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )

        return Response(
            {
                "status": "success",
                "code": 200,
                "data": GroupHomeSerializer(group_home).data,
            }
        )

    # ==================================================
    # SHARED ROOM UPDATE LOGIC (USED BY PUT & PATCH)
    # ==================================================
    # ✔ Handles increase in rooms
    # ✔ Handles decrease in rooms
    # ✔ Prevents deletion of occupied rooms
    # ✔ Updates no_of_rooms safely
    # ==================================================
    def _handle_room_update(self, group_home, new_rooms):
        old_rooms = group_home.no_of_rooms
        new_rooms = int(new_rooms)

        # ➕ Increase rooms
        if new_rooms > old_rooms:
            GroupHomeRoom.objects.bulk_create([
                GroupHomeRoom(
                    group_home=group_home,
                    room_number=str(old_rooms + i + 1)
                )
                for i in range(new_rooms - old_rooms)
            ])

        # ➖ Decrease rooms
        elif new_rooms < old_rooms:
            room_numbers_to_remove = list(
                range(new_rooms + 1, old_rooms + 1)
            )

            # ❌ Do not remove occupied rooms
            if GroupHomeRoom.objects.filter(
                group_home=group_home,
                room_number__in=room_numbers_to_remove,
                is_occupied=True
            ).exists():
                return False, "Cannot remove occupied rooms"

            # ✅ Safe delete
            GroupHomeRoom.objects.filter(
                group_home=group_home,
                room_number__in=room_numbers_to_remove
            ).delete()

        # ✅ Update count
        group_home.no_of_rooms = new_rooms
        group_home.save(update_fields=["no_of_rooms"])

        return True, None

    # -------------------------------------------------
    # FULL UPDATE (PUT)
    # -------------------------------------------------
    # ✔ Requires full payload
    # ✔ Updates rooms first
    # ✔ Replaces nested objects if provided
    # -------------------------------------------------
    @extend_schema(
        operation_id="update_group_home",
        summary="Update group home (full update)",
        description="Fully update a group home. All required fields must be provided.",
        request=GroupHomeSerializer,
        responses={200: GroupHomeSerializer},
        tags=["Group Home"],
    )
    @staticmethod
    def _snapshot_group_home(gh):
        """Capture core field values for change detection (excludes media)."""
        snap = {
            "name": gh.name,
            "timezone": gh.timezone,
            "phone": gh.phone,
            "fax": gh.fax,
            "email": gh.email,
            "emergency_contact_number": gh.emergency_contact_number,
            "no_of_rooms": gh.no_of_rooms,
            "active": gh.active,
            "address_id": gh.address_id,
            "license_id": gh.license_id,
        }
        if gh.address:
            snap["address"] = (
                gh.address.line1, gh.address.line2,
                gh.address.city, gh.address.state,
                gh.address.zipcode, gh.address.country,
            )
        if gh.license:
            snap["license"] = (
                gh.license.number, gh.license.start_date,
                gh.license.expiry_date,
            )
        snap["shifts"] = tuple(
            gh.shifts.order_by("shift").values_list("shift", "start_time", "end_time", "is_active")
        )
        return snap

    @transaction.atomic
    def put(self, request, uuid):
        group_home = self.get_object(uuid)
        if not group_home:
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )

        # Scope check: ASSIGNED_HOME users can only update their own home
        if not check_scope_access(request.user, group_home, "group_homes.edit", "__self__"):
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )

        # Snapshot before update to detect real changes
        before = self._snapshot_group_home(group_home)

        # Handle room changes before serializer update
        if "no_of_rooms" in request.data:
            success, error = self._handle_room_update(
                group_home,
                request.data["no_of_rooms"]
            )
            if not success:
                return Response(
                    {"status": "error", "message": error},
                    status=400,
                )

        serializer = GroupHomeSerializer(
            group_home,
            data=request.data,
            partial=False  # PUT = full update
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # Refresh related objects for accurate comparison
        group_home.refresh_from_db()
        if group_home.address:
            group_home.address.refresh_from_db()
        if group_home.license:
            group_home.license.refresh_from_db()
        after = self._snapshot_group_home(group_home)

        # Only log when actual group home data changed (skip media-only updates)
        if before != after:
            log_action(
                request=request,
                action="UPDATE",
                entity_type="GroupHome",
                entity_id=str(group_home.uuid),
                group_home=group_home,
                message="Group home updated",
            )

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Group home updated successfully",
                "data": GroupHomeSerializer(group_home).data,
            }
        )


    # -------------------------------------------------
    # SOFT DELETE GROUP HOME
    # -------------------------------------------------
    @extend_schema(
        operation_id="delete_group_home",
        summary="Delete group home",
        description="Soft delete a group home.",
        tags=["Group Home"],
        parameters=[
            OpenApiParameter(
                name="uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="Group Home UUID"
            )
        ],
        responses={200: None},
    )
    def delete(self, request, uuid):
        group_home = self.get_object(uuid)
        if not group_home:
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )

        from group_home.guards import group_home_blockers

        can, blockers = group_home_blockers(group_home)
        if not can:
            return Response(
                {"status": "error", "error": "Cannot deactivate/delete — remove blockers first", "blockers": blockers},
                status=409,
            )

        has_active_residents = LeadGroupHomeAssignment.objects.filter(
            group_home=group_home,
            status__in=["ASSIGNED", "ACTIVE"],
        ).exists()

        if has_active_residents:
            return Response(
                {
                    "status": "error",
                    "message": "Cannot delete group home with active or assigned residents. Please move out all residents before deleting.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        group_home.deleted_at = timezone.now()
        group_home.save(update_fields=["deleted_at"])

        log_action(
            request=request,
            action="DELETE",
            entity_type="GroupHome",
            entity_id=str(group_home.uuid),
            group_home=group_home,
            message="Group home deleted",
        )

        return Response(
            {
                "status": "success",
                "message": "Group home deleted successfully",
            },
            status=status.HTTP_200_OK,
        )

        

# =====================================================
# GROUP HOME STATUS UPDATE (PATCH ONLY)
# =====================================================
class GroupHomeStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PATCH": "group_homes.deactivate",  # Admin, PD only
    }
    # No portal access — portal users cannot deactivate group homes

    # -------------------------------------------------
    # Helper: Fetch group home by UUID
    # -------------------------------------------------
    def get_object(self, uuid):
        try:
            return GroupHome.objects.get(
                uuid=uuid,
                deleted_at__isnull=True
            )
        except GroupHome.DoesNotExist:
            return None

    # -------------------------------------------------
    # UPDATE GROUP HOME STATUS (PATCH)
    # -------------------------------------------------
    @extend_schema(
        operation_id="update_group_home_status",
        summary="Activate / Deactivate group home",
        description="Update the active status of a group home.",
        tags=["Group Home"],
        parameters=[
            OpenApiParameter(
                name="uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description="Group Home UUID"
            )
        ],
        request=GroupHomeStatusSerializer,
        responses={200: GroupHomeStatusSerializer},
    )
    @transaction.atomic
    def patch(self, request, uuid):
        group_home = self.get_object(uuid)
        if not group_home:
            return Response(
                {
                    "status": "error",
                    "message": "Group home not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # Scope check: ASSIGNED_HOME users can only update their own home
        if not check_scope_access(request.user, group_home, "group_homes.edit", "__self__"):
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Block deactivation when active residents exist
        new_active = request.data.get("active")
        if new_active is False and (group_home.active is True):
            from group_home.guards import group_home_blockers

            can, blockers = group_home_blockers(group_home)
            if not can:
                return Response(
                    {"status": "error", "error": "Cannot deactivate/delete — remove blockers first", "blockers": blockers},
                    status=409,
                )

            has_active_residents = LeadGroupHomeAssignment.objects.filter(
                group_home=group_home,
                status__in=["ASSIGNED", "ACTIVE"],
            ).exists()

            if has_active_residents:
                return Response(
                    {
                        "status": "error",
                        "message": "Cannot deactivate group home with active or assigned residents. Please move out all residents before deactivating.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        serializer = GroupHomeStatusSerializer(
            group_home,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        log_action(
            request=request,
            action="STATUS_UPDATE",
            entity_type="GroupHome",
            entity_id=str(group_home.uuid),
            group_home=group_home,
            message=(
                "Group home activated"
                if group_home.active
                else "Group home deactivated"
            ),
        )

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Group home status updated successfully",
                "data": {
                    "uuid": str(group_home.uuid),
                    "active": group_home.active,
                },
            },
            status=status.HTTP_200_OK,
        )


# =====================================================
# GROUP HOME USERS LIST API
# =====================================================
class GroupHomeUsersAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "group_homes.view_profile",  # Must be able to see the home profile to list its users
    }
    # No portal access — G/A don't need to browse staff user lists

    # -------------------------------------------------
    # Helper: Fetch group home by UUID
    # -------------------------------------------------
    def get_group_home(self, uuid):
        try:
            return GroupHome.objects.get(uuid=uuid, deleted_at__isnull=True)
        except GroupHome.DoesNotExist:
            return None

    # -------------------------------------------------
    # GET USERS (with role_type, active filters & pagination)
    # -------------------------------------------------
    @extend_schema(
        operation_id="list_group_home_users",
        summary="List users of a group home",
        description="Fetch users for a group home with role_type (comma-separated) and active filters, plus pagination.",
        tags=["Group Home"],
        parameters=[
            OpenApiParameter(
                name="role_type",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Comma-separated role types: staff,admin,lead,resident,guardian,agent"
            ),
            OpenApiParameter(
                name="active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description="Filter by active status (true/false)"
            ),
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page number"
            ),
            OpenApiParameter(
                name="size",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page size"
            ),
        ],
        responses={200: GroupHomeUserSerializer(many=True)},
    )
    def get(self, request, uuid):
        # ----- Fetch group home -----
        group_home = self.get_group_home(uuid)
        if not group_home:
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ----- Base queryset -----
        queryset = User.objects.filter(
            Q(group_home_id=group_home.id) | Q(group_homes=group_home),
            deleted_at__isnull=True
        ).select_related("role").distinct()

        # ----- Filter by role_type (comma-separated, case-insensitive) -----
        role_param = request.query_params.get("role_type")
        if role_param:
            role_types = [r.strip() for r in role_param.split(",")]
            q = Q()
            for rt in role_types:
                q |= Q(role__type__iexact=rt)
            queryset = queryset.filter(q)

        # ----- Filter by active status -----
        active_param = request.query_params.get("active")
        if active_param is not None:
            active_bool = active_param.lower() == "true"
            queryset = queryset.filter(active=active_bool)

        # ----- Pagination -----
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))
        paginator = Paginator(queryset.order_by("-created_at"), size)
        paginated_data = paginator.get_page(page)

        # ----- Serialize and return response -----
        serializer = GroupHomeUserSerializer(paginated_data, many=True)
        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Users fetched successfully",
                "data": {
                    "results": serializer.data,
                    "pagination": {
                        "page": page,
                        "size": size,
                        "total_pages": paginator.num_pages,
                        "total_records": paginator.count,
                    },
                },
            }
        )


# =====================================================
# GROUP HOME STAFF ASSIGNMENTS API (LIST + CREATE)
# =====================================================
class GroupHomeAssignmentListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "group_homes.view",
        "POST": "group_homes.assign_staff",
    }

    def _get_home(self, uuid):
        try:
            return GroupHome.objects.get(uuid=uuid, deleted_at__isnull=True)
        except GroupHome.DoesNotExist:
            return None

    def get(self, request, uuid):
        home = self._get_home(uuid)
        if not home:
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )
        role = request.query_params.get("role")
        qs = home.staff_assignments.filter(status="ACTIVE").select_related("user")
        if role:
            qs = qs.filter(role_type=role.upper())
        data = GroupHomeStaffAssignmentSerializer(qs, many=True).data
        return Response(
            {"status": "success", "data": data},
            status=200,
        )

    def post(self, request, uuid):
        home = self._get_home(uuid)
        if not home:
            return Response(
                {"status": "error", "message": "Group home not found"},
                status=404,
            )

        role_type = (request.data.get("role_type") or "").upper()
        valid = {c[0] for c in GroupHomeStaffAssignment.RoleType.choices}
        if role_type not in valid:
            return Response(
                {"status": "error", "errors": {"role_type": f"Must be one of: {sorted(valid)}"}},
                status=400,
            )

        ser = GroupHomeStaffAssignmentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = User.objects.get(uuid=request.data["user_uuid"])

        assignment, created = GroupHomeStaffAssignment.objects.get_or_create(
            group_home=home, user=user, role_type=role_type,
            defaults={"status": "ACTIVE"},
        )
        if not created and assignment.status != "ACTIVE":
            assignment.status = "ACTIVE"
            assignment.deleted_at = None
            assignment.save(update_fields=["status", "deleted_at", "updated_at"])
            code = status.HTTP_200_OK
        elif not created:
            code = status.HTTP_200_OK
        else:
            code = status.HTTP_201_CREATED

        data = GroupHomeStaffAssignmentSerializer(assignment).data
        return Response({"status": "success", "data": data}, status=code)


# =====================================================
# GROUP HOME STAFF ASSIGNMENT DETAIL (DELETE)
# =====================================================
class GroupHomeAssignmentDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"DELETE": "group_homes.assign_staff"}

    def delete(self, request, uuid, assignment_uuid):
        try:
            assignment = GroupHomeStaffAssignment.objects.get(
                uuid=assignment_uuid, group_home__uuid=uuid, status="ACTIVE",
            )
        except GroupHomeStaffAssignment.DoesNotExist:
            return Response(
                {"status": "error", "message": "Assignment not found"},
                status=404,
            )

        assignment.status = "INACTIVE"
        assignment.deleted_at = timezone.now()
        assignment.save(update_fields=["status", "deleted_at", "updated_at"])
        return Response(
            {"status": "success", "message": "Assignment removed"},
            status=200,
        )


# =====================================================
# GROUP HOME CAN-DELETE PREFLIGHT
# =====================================================
from group_home.guards import group_home_blockers


class GroupHomeCanDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"GET": "group_homes.deactivate"}

    def get(self, request, uuid):
        try:
            home = GroupHome.objects.get(uuid=uuid, deleted_at__isnull=True)
        except GroupHome.DoesNotExist:
            return Response({"status": "error", "message": "Group home not found"}, status=404)
        can, blockers = group_home_blockers(home)
        return Response({"can_delete": can, "blockers": blockers}, status=200)