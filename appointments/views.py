from re import search
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.db import transaction
from .models import Appointment
from django.db.models import Q
from audit_logs.utils import log_action, get_active_group_home
from django.utils.dateparse import parse_date
from django.shortcuts import get_object_or_404
from django.core.paginator import Paginator
from leads.models import LeadGroupHomeAssignment
from accounts.permissions import scope_queryset, scope_queryset_portal, check_scope_access, HasPermission
from .serializers import AppointmentSerializer,AppointmentListSerializer
from accounts.email_service import send_email 
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from django.db.models import Prefetch


class AppointmentAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "appointments.view",
        "POST": "appointments.create",
    }
    allow_portal_roles = {"GET"}  # Guardians/Agents can view their appointments

    @extend_schema(
        operation_id="create_appointment",
        summary="Create appointment",
        description=(
            "Create a new appointment for a resident. "
            "Optionally sends email notification to provider or guardian."
        ),
        request=AppointmentSerializer,
        responses={
            201: AppointmentSerializer,
            400: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
        tags=["Appointments"],
    )
  
    def post(self, request):
        serializer = AppointmentSerializer(
            data=request.data,
            context={"request": request}
        )

        if not serializer.is_valid():
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                appointment = serializer.save(created_by=request.user if request.user.is_authenticated else None)

                log_action(
                        request=request,
                        action="CREATE",
                        entity_type="Appointment",
                        entity_id=str(appointment.uuid),
                        group_home=get_active_group_home(appointment.lead),
                        message="Appointment created",
                    )
                # ------------------ SEND EMAIL ------------------
                if appointment.contact_email:
                    self.send_appointment_email(appointment)


            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_201_CREATED,
                    "message": "Appointment created successfully",
                    "data": AppointmentSerializer(appointment).data,
                },
                status=status.HTTP_201_CREATED
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to create appointment",
                    "errors": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    # ------------------ EMAIL HANDLER ------------------

    def send_appointment_email(self, appointment):
        subject = "New Appointment Scheduled"
        context = {
            "appointment_title": appointment.appointment_title,
            "appointment_date": appointment.appointment_date,
            "appointment_time": appointment.appointment_time,
            "resident_name": (
                f"{appointment.lead.user.first_name} "
                f"{appointment.lead.user.last_name}"
            ),
            "contact_name": appointment.contact_name,
            "description": appointment.description,
        }

        send_email(
            to_email=appointment.contact_email,
            subject=subject,
            template_name="appointment_created.html",
            context=context
        )

#---------------------AppointmentListAPIView---------------------------------------------
    @extend_schema(
        operation_id="list_appointments",
        summary="List appointments",
        description=(
            "Retrieve a paginated list of appointments. "
            "Supports filtering by resident name, group home, status, and date."
        ),
        tags=["Appointments"],
        parameters=[
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page number",
                required=False,
                default=1,
            ),
            OpenApiParameter(
                name="size",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Number of records per page",
                required=False,
                default=10,
            ),
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Search by resident first or last name",
                required=False,
            ),
            OpenApiParameter(
                name="group_home",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Filter by active group home ID",
                required=False,
            ),
            OpenApiParameter(
                name="status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by appointment status",
                required=False,
            ),
            OpenApiParameter(
                name="date",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Filter by appointment date (YYYY-MM-DD)",
                required=False,
            ),
             OpenApiParameter(
                name="lead_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter appointments by resident (lead) UUID",
            ),
            OpenApiParameter(
                name="user_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter appointments by guardian or agent UUID",
                required=False,
            ),
            
        ],
        responses={200: OpenApiTypes.OBJECT},
    )

    def get(self, request):
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))

        appointments = (
            Appointment.objects
            .select_related("lead__user")
            .prefetch_related(
                Prefetch(
                    "lead__group_home_assignments",
                    queryset=LeadGroupHomeAssignment.objects.filter(
                        status="ACTIVE"
                    ).select_related("room", "group_home"),
                    to_attr="active_assignment",
                )
            )
            .prefetch_related("media_items")
            .filter(deleted_at__isnull=True)
            .order_by("-appointment_date", "-appointment_time")
        )

        # Scope to assigned group home for ASSIGNED_HOME users
        appointments = scope_queryset(
            request.user, appointments, "appointments.view",
            group_home_path="lead__group_home_assignments__group_home",
        )

        # SECURITY: GUARDIAN/AGENT portal users are scoped to their OWN linked
        # residents, enforced from request.user — independent of the optional
        # ?user_uuid= filter below. Without this a portal user who omits that
        # param sees every resident's appointments.
        appointments = scope_queryset_portal(
            request.user, appointments,
            guardian_path="lead__guardian_id",
            agent_path="lead__agent_id",
            active_assignment_path="lead__group_home_assignments__status",
        )

        # ---------------- SEARCH (Resident Name) ----------------
        search = request.query_params.get("search")

        if search:
            search_terms = search.strip().split()

            query = Q()
            for term in search_terms:
                query &= (
                    Q(lead__user__first_name__icontains=term) |
                    Q(lead__user__last_name__icontains=term)
                )

            appointments = appointments.filter(query)

       # ---------------- FILTER: GROUP HOME ----------------
        group_home = request.query_params.get("group_home")
        if group_home:
            appointments = appointments.filter(
                lead__group_home_assignments__group_home__uuid=group_home,
                lead__group_home_assignments__status="ACTIVE",
        ).distinct()

        # ---------------- FILTER: STATUS ----------------
        status_param = request.query_params.get("status")
        if status_param:
            appointments = appointments.filter(status=status_param)

        # ---------------- FILTER: DATE ----------------
        date = request.query_params.get("date")
        if date:
            parsed_date = parse_date(date)
            if parsed_date:
                appointments = appointments.filter(appointment_date=parsed_date)

        # ---------------- FILTER: LEAD UUID (Resident) ----------------
        lead_uuid = request.query_params.get("lead_uuid")
        if lead_uuid:
            appointments = appointments.filter(lead__uuid=lead_uuid)

        # ---------------- FILTER: GUARDIAN OR AGENT UUID ----------------
        user_uuid = request.query_params.get("user_uuid")
        if user_uuid:
            appointments = appointments.filter(
                Q(lead__guardian__uuid=user_uuid) |
                Q(lead__agent__uuid=user_uuid)
            )
            # Exclude moved-out residents for Guardian and Agent portals
            appointments = appointments.filter(
                lead__group_home_assignments__status="ACTIVE"
            )
            appointments = appointments.order_by(
                "lead_id",                 # required for distinct
                "-appointment_date",
                "-appointment_time",
            ).distinct("lead_id")
        paginator = Paginator(appointments, size)
        page_obj = paginator.get_page(page)

        serializer = AppointmentSerializer(page_obj, many=True)



        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Appointments fetched successfully",
                "data": {
                    "results": serializer.data,
                    "pagination": {
                        "page": page_obj.number,
                        "size": size,
                        "total_pages": paginator.num_pages,
                        "total_records": paginator.count,
                        "has_next": page_obj.has_next(),
                        "has_previous": page_obj.has_previous(),
                    },
                },
            },
            status=status.HTTP_200_OK
        )
    
#---------------------------AppointmentDetailAPIView--------------------------------------------------------

class AppointmentDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":    "appointments.view",
        "PUT":    "appointments.edit",
        "DELETE": "appointments.delete",
    }
    allow_portal_roles = {"GET"}  # Guardians/Agents can view appointment details

    @extend_schema(
        operation_id="get_appointment",
        summary="Get appointment details",
        description="Retrieve appointment details using appointment UUID.",
        tags=["Appointments"],
        responses={
            200: AppointmentSerializer,
            404: OpenApiTypes.OBJECT,
        },
    )

    def get(self, request, uuid):

        active_assignment_qs = LeadGroupHomeAssignment.objects.filter(
            status="ACTIVE"
        ).select_related("room", "group_home")

        appointment = (
                Appointment.objects
                .filter(uuid=uuid, deleted_at__isnull=True)
                .select_related("lead", "lead__user")
                .prefetch_related(
                    "media_items",
                    Prefetch(
                        "lead__group_home_assignments",
                        queryset=active_assignment_qs,
                        to_attr="active_assignment"   # 👈 important
                    )
                )  # ✅ REQUIRED
                .first()
            )

        if not appointment:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Appointment not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND
            )

        # Scope check: ASSIGNED_HOME users can only view appointments for their residents
        if not check_scope_access(
            request.user, appointment, "appointments.view",
            group_home_path="lead__group_home_assignments__group_home",
        ):
            return Response(
                {"status": "error", "code": status.HTTP_404_NOT_FOUND, "message": "Appointment not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = AppointmentSerializer(
            appointment,
            context={"request": request}  # keeps avatar/media URLs working
        )

        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Appointment fetched successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK
        )
    
    # ---------------- PUT ----------------
    @extend_schema(
        operation_id="update_appointment",
        summary="Update appointment",
        description="Update an existing appointment using appointment UUID.",
        request=AppointmentSerializer,
        tags=["Appointments"],
        responses={
            200: AppointmentSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    def put(self, request, uuid):
        appointment = Appointment.objects.filter(
            uuid=uuid,
            deleted_at__isnull=True
        ).first()

        if not appointment:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Appointment not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND
            )

        # Scope check: ASSIGNED_HOME users can only edit appointments for their residents
        if not check_scope_access(
            request.user, appointment, "appointments.edit",
            group_home_path="lead__group_home_assignments__group_home",
        ):
            return Response(
                {"status": "error", "code": status.HTTP_404_NOT_FOUND, "message": "Appointment not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = AppointmentSerializer(
            appointment,
            data=request.data,
            partial=True,  
            context={"request": request}
        )

        if not serializer.is_valid():
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                updated_appointment = serializer.save()

            log_action(
                request=request,
                action="UPDATE",
                entity_type="Appointment",
                entity_id=str(updated_appointment.uuid),
                group_home=get_active_group_home(updated_appointment.lead),
                message="Appointment updated",
            )

            # Send update notification email
            if updated_appointment.contact_email:
                staff_name = ""
                if request.user and request.user.is_authenticated:
                    staff_name = f"{request.user.first_name} {request.user.last_name}".strip()

                context = {
                    "appointment_title": updated_appointment.appointment_title,
                    "appointment_date": updated_appointment.appointment_date,
                    "appointment_time": updated_appointment.appointment_time,
                    "resident_name": (
                        f"{updated_appointment.lead.user.first_name} "
                        f"{updated_appointment.lead.user.last_name}"
                    ),
                    "contact_name": updated_appointment.contact_name,
                    "description": updated_appointment.description,
                    "updated_by": staff_name,
                }

                send_email(
                    to_email=updated_appointment.contact_email,
                    subject="Appointment Updated",
                    template_name="appointment_updated.html",
                    context=context,
                )

            return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Appointment updated successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK
        )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to update appointment",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
    # ---------------- DELETE (SOFT DELETE) ----------------

    @extend_schema(
        operation_id="delete_appointment",
        summary="Delete appointment",
        description="Soft delete an appointment using appointment UUID.",
        tags=["Appointments"],
        responses={
            200: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    def delete(self, request, uuid):
        appointment = Appointment.objects.filter(
            uuid=uuid,
            deleted_at__isnull=True
        ).first()

        if not appointment:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Appointment not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND
            )

        # Scope check: ASSIGNED_HOME users can only delete appointments for their residents
        if not check_scope_access(
            request.user, appointment, "appointments.delete",
            group_home_path="lead__group_home_assignments__group_home",
        ):
            return Response(
                {"status": "error", "code": status.HTTP_404_NOT_FOUND, "message": "Appointment not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            with transaction.atomic():
                appointment.deleted_at = timezone.now()
                appointment.save(update_fields=["deleted_at", "updated_at"])

            log_action(
                request=request,
                action="DELETE",
                entity_type="Appointment",
                entity_id=str(appointment.uuid),
                group_home=get_active_group_home(appointment.lead),
                message="Appointment deleted",
            )


            return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Appointment deleted successfully",
                "data": None,
            },
            status=status.HTTP_200_OK
        )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to delete appointment",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

#--------------------------------------------------------------------------------------
class AppointmentStatusUpdateAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    # User needs EITHER mark_completed (DSP) OR edit (Nurse/Coordinator/etc.)
    # The internal check_scope_access already enforces the finer OR logic per object.
    allow_any_permissions = {
        "PATCH": ["appointments.mark_completed", "appointments.edit"],
    }
    # authentication_classes = [JWTAuthentication]

    ALLOWED_TRANSITIONS = {
        "REQUESTED": ["COMPLETED", "CANCELLED"],
        "COMPLETED": [],
        "CANCELLED": [],
    }

    @extend_schema(
        operation_id="update_appointment_status",
        summary="Update appointment status",
        description=(
            "Update the status of an appointment with validation rules. "
            "Allowed transitions: REQUESTED → COMPLETED / CANCELLED."
        ),
        request=OpenApiTypes.OBJECT,
        tags=["Appointments"],
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )

    def patch(self, request, uuid):
        appointment = Appointment.objects.filter(
        uuid=uuid,
        deleted_at__isnull=True
        ).first()

        if not appointment:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Appointment not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND
            )

        # Scope check: ASSIGNED_HOME users can only update appointments for their residents
        # Accept either appointments.edit OR appointments.mark_completed permission
        # (DSP users have mark_completed but not edit)
        has_edit_access = check_scope_access(
            request.user, appointment, "appointments.edit",
            group_home_path="lead__group_home_assignments__group_home",
        )
        has_mark_completed_access = check_scope_access(
            request.user, appointment, "appointments.mark_completed",
            group_home_path="lead__group_home_assignments__group_home",
        )
        if not has_edit_access and not has_mark_completed_access:
            return Response(
                {"status": "error", "code": status.HTTP_403_FORBIDDEN, "message": "You do not have permission to perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )

        new_status = request.data.get("status")
        action_note = request.data.get("action_note")

        if not new_status:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Status is required",
                    "errors": {"field": "status"},
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        
        if not action_note:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Action note is required",
                    "errors": {"field": "action_note"},
                },
                status=status.HTTP_400_BAD_REQUEST
            )   
        if new_status not in self.ALLOWED_TRANSITIONS.get(appointment.status, []):
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Invalid status transition",
                    "errors": {
                        "current_status": appointment.status,
                        "requested_status": new_status,
                    },
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        if new_status == "COMPLETED" and appointment.appointment_date:
            # Check if it's an early completion for logging purposes
            today = timezone.now().date()
            is_early = appointment.appointment_date > today
            
            # We no longer block early completion per user request.
            # If it were time-sensitive too, we'd check appointment_time,
            # but the previous restriction was only on date.
            
            log_suffix = " (Early completion)" if is_early else ""
            
        else:
            log_suffix = ""

        appointment.status = new_status
        appointment.action_note = action_note
        appointment.action_by = (request.user if request.user.is_authenticated else None)
        appointment.action_at = timezone.now()
        appointment.save()

        log_action(
            request=request,
            action="UPDATE",
            entity_type="Appointment",
            entity_id=str(appointment.uuid),
            group_home=get_active_group_home(appointment.lead),
            message=f"Appointment status changed to {appointment.status}{log_suffix}",
        )

        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Appointment status updated successfully",
                "data": {
                    "uuid": str(appointment.uuid),
                    "status": appointment.status,
                    "action_at": appointment.action_at,
                },
            },
            status=status.HTTP_200_OK
        )