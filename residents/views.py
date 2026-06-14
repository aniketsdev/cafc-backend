"""
Commit: residents/views.py
--------------------------
Purpose:
- APIViews for Residents Care Plan module
- Handles ADLs & Goals lifecycle
- Handles daily tracking (logs)
- Implements filtering, archiving, and history
- Swagger documented using drf-spectacular
"""

from urllib import request
from django.utils import timezone
from audit_logs.utils import log_audit
from django.shortcuts import get_object_or_404

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from django.db.models import Prefetch, Exists, OuterRef
from datetime import datetime, date, timedelta
from collections import defaultdict
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import HasPermission, scope_queryset
from rest_framework.pagination import PageNumberPagination








from .models import CarePlanItem, CarePlanDailyLog
from .serializers import (
    CarePlanItemSerializer,
    CarePlanDailyLogSerializer,
    CarePlanItemListSerializer,
    CarePlanDailyLogHistorySerializer,
)
from .models import CarePlanReport
from .serializers import CarePlanReportSerializer
from .models import ResidentScheduledForm
from .serializers import ResidentScheduledFormSerializer



# ==========================================================
# Commit 1: Care Plan Items (ADLs / Goals)
# ==========================================================


class CarePlanItemListCreateAPIView(APIView):
    """
    List & Create ADLs / Goals for residents
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "adls.view",
        "POST": "adls.edit",
    }
    # Agents & Guardians can view care plan items for their residents
    allow_portal_roles = True

    @extend_schema(
        summary="List Care Plan Items (ADLs / Goals)",
        parameters=[
            OpenApiParameter("resident_uuid", OpenApiTypes.STR, required=False),
            OpenApiParameter(
                "type", OpenApiTypes.STR, required=False, enum=["ADL", "GOAL"]
            ),
            OpenApiParameter("archived", OpenApiTypes.BOOL, required=False),
            OpenApiParameter("search", OpenApiTypes.STR, required=False),
            # Daily tracking
            OpenApiParameter("date", OpenApiTypes.DATE, required=False),
            OpenApiParameter("shifts", OpenApiTypes.STR, required=False),
            # Monthly summary
            OpenApiParameter("month", OpenApiTypes.INT, required=False),
            OpenApiParameter("year", OpenApiTypes.INT, required=False),
        ],
        responses=CarePlanItemListSerializer(many=True),
        tags=["Residents - Care Plan"],
    )

    
    def get(self, request):
    # -------------------------------
    # Query parameters
    # -------------------------------
        resident_uuid = request.query_params.get("resident_uuid")
        item_type = request.query_params.get("type")  # ADL / GOAL
        shifts_param = request.query_params.get("shifts")
        date_param = request.query_params.get("date")
        month = request.query_params.get("month")
        year = request.query_params.get("year")
        archived = request.query_params.get("archived")
        search = request.query_params.get("search")

        if not resident_uuid:
            return Response(
                {"status": "error", "message": "resident_uuid is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # -------------------------------
        # Base queryset
        # -------------------------------
        queryset = CarePlanItem.objects.filter(resident__uuid=resident_uuid)

        # Scope: ASSIGNED_HOME users only see care plan items for residents in their group home
        queryset = scope_queryset(
            request.user, queryset, "adls.view",
            group_home_path="resident__leads__group_home_assignments__group_home",
        )

        # -------------------------------
        # Filters
        # -------------------------------
        if item_type:
            queryset = queryset.filter(type=item_type)

        if archived is not None:
            if archived.lower() == "true":
                queryset = queryset.filter(deleted_at__isnull=False)
            elif archived.lower() == "false":
                queryset = queryset.filter(deleted_at__isnull=True)
        else:
            queryset = queryset.filter(deleted_at__isnull=True)

        if search:
            queryset = queryset.filter(title__icontains=search)

        if shifts_param:
            shift_list = [s.strip().upper() for s in shifts_param.split(",")]
            queryset = queryset.filter(
                assigned_shifts__shift__in=shift_list
            ).distinct()
        else:
            shift_list = None

            

        # -------------------------------
        # Role-based filtering for GUARDIAN and AGENT
        # -------------------------------
        user_role = request.user.role.type.upper() if hasattr(request.user, 'role') and request.user.role else ""
        if date_param and user_role in ["GUARDIAN", "AGENT"]:
            try:
                parsed_date = datetime.strptime(date_param, "%Y-%m-%d").date()
                log_exists = CarePlanDailyLog.objects.filter(
                    care_plan_item=OuterRef('pk'),
                    resident__uuid=resident_uuid,
                    log_date=parsed_date,
                    deleted_at__isnull=True
                )
                if shift_list:
                    log_exists = log_exists.filter(shift__in=shift_list)
                
                queryset = queryset.filter(Exists(log_exists))
            except ValueError:
                # Date format error will be handled by the daily_log_filters parsing below
                pass

        daily_log_filters = {
            "resident__uuid": resident_uuid,
            "deleted_at__isnull": True,
        }

        if date_param:
            try:
                daily_log_filters["log_date"] = datetime.strptime(date_param, "%Y-%m-%d").date()
            except ValueError:
                return Response(
                    {"status": "error", "message": "Invalid date format, use YYYY-MM-DD"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if shift_list:
            daily_log_filters["shift__in"] = shift_list

        daily_logs_prefetch = Prefetch(
            "daily_logs",
            queryset=CarePlanDailyLog.objects.filter(**daily_log_filters),
            to_attr="filtered_daily_logs"
        )

        queryset = queryset.select_related(
            "created_by",
            "created_by__role",
            "updated_by",
            "updated_by__role",
        ).prefetch_related(
            "assigned_shifts",
            daily_logs_prefetch
        )

        # -------------------------------
        # Serialize
        # -------------------------------
        serializer = CarePlanItemListSerializer(
            queryset,
            many=True,
            context={"request": request, "resident_uuid": resident_uuid},
        )

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Care plan items fetched successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )




    

    @extend_schema(
        summary="Create Care Plan Item (ADL / Goal)",
        request=CarePlanItemSerializer,
        responses=CarePlanItemSerializer,
        tags=["Residents - Care Plan"],
    )
    def post(self, request):
        serializer = CarePlanItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = serializer.save(created_by=request.user)

        log_audit(
            request=request,
            action="CREATE",
            entity_type="CarePlanItem",
            entity_id=str(item.uuid),
            message="Care plan item created",
        )

        return Response(
            {
                "status": "success",
                "code": 201,
                "message": "Care plan item created successfully",
                "data": CarePlanItemSerializer(item).data,
            },
            status=status.HTTP_201_CREATED,
        )


class CarePlanItemDetailAPIView(APIView):
    """
    Retrieve / Update Care Plan Item
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "adls.view",
        "PUT": "adls.edit",
    }
    # Agents & Guardians can view individual care plan item details
    allow_portal_roles = True

    def get_object(self, uuid):
        return get_object_or_404(CarePlanItem, uuid=uuid)

    @extend_schema(
        summary="Get Care Plan Item detail / history",
        parameters=[
            OpenApiParameter(
                name="view",
                type=OpenApiTypes.STR,
                required=False,
                enum=["history"],
                description="Pass 'history' to get past-to-present daily logs",
            )
        ],
        responses=CarePlanItemSerializer,
        tags=["Residents - Care Plan"],
    )
    def get(self, request, uuid):
        item = self.get_object(uuid)

        view_type = request.query_params.get("view")

        # ✅ HISTORY VIEW
        if view_type == "history":
            logs = CarePlanDailyLog.objects.filter(
                care_plan_item=item, deleted_at__isnull=True
            ).order_by("log_date", "shift")

            serializer = CarePlanDailyLogHistorySerializer(logs, many=True)

            return Response(
                {
                    "status": "success",
                    "code": 200,
                    "data": {
                        "care_plan_item": {
                            "uuid": str(item.uuid),
                            "title": item.title,
                            "type": item.type,
                        },
                        "history": serializer.data,
                    },
                }
            )

        # ✅ DEFAULT DETAIL (UNCHANGED)
        return Response(
            {
                "status": "success",
                "code": 200,
                "data": CarePlanItemSerializer(item).data,
            }
        )

    @extend_schema(
        summary="Update Care Plan Item",
        request=CarePlanItemSerializer,
        responses=CarePlanItemSerializer,
        tags=["Residents - Care Plan"],
    )
    def put(self, request, uuid):
        item = self.get_object(uuid)
        serializer = CarePlanItemSerializer(item, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)

        log_audit(
            request=request,
            action="UPDATE",
            entity_type="CarePlanItem",
            entity_id=str(item.uuid),
            message="Care plan item updated",
        )

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Care plan item updated successfully",
                "data": serializer.data,
            }
        )


class CarePlanItemArchiveAPIView(APIView):
    """
    Archive (soft delete) a Care Plan Item
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "adls.edit",
    }

    @extend_schema(
        summary="Archive Care Plan Item",
        responses={204: None},
        tags=["Residents - Care Plan"],
    )
    def post(self, request, uuid):
        item = get_object_or_404(CarePlanItem, uuid=uuid)
        item.deleted_at = timezone.now()
        item.updated_by = request.user
        item.save(update_fields=["deleted_at", "updated_by"])

        log_audit(
            request=request,
            action="DELETE",
            message="Care plan item archived",
            entity_type="CarePlanItem",
            entity_id=str(item.uuid),
        )

        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================================
# Commit 2: Care Plan Daily Logs (Tracking)
# ==========================================================


class CarePlanDailyLogListCreateAPIView(APIView):
    """
    List & Create Daily Logs
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "adls.view",
        "POST": "adls.record",
    }
    # Agents & Guardians can view daily logs for their residents
    allow_portal_roles = True

    @extend_schema(
        summary="List Daily Logs",
        parameters=[
            OpenApiParameter("resident_uuid", OpenApiTypes.STR, required=False),
            OpenApiParameter("date", OpenApiTypes.DATE, required=False),
            OpenApiParameter("care_plan_item_uuid", OpenApiTypes.STR, required=False),
            OpenApiParameter("archived", OpenApiTypes.BOOL, required=False),
        ],
        responses=CarePlanDailyLogSerializer(many=True),
        tags=["Residents - Daily Logs"],
    )
    def get(self, request):
        queryset = CarePlanDailyLog._base_manager.all()

        resident_uuid = request.query_params.get("resident_uuid")
        log_date = request.query_params.get("date")
        care_plan_item_uuid = request.query_params.get("care_plan_item_uuid")
        archived = request.query_params.get("archived")

        if resident_uuid:
            queryset = queryset.filter(resident__uuid=resident_uuid)

        # Scope: ASSIGNED_HOME users only see daily logs for residents in their group home
        queryset = scope_queryset(
            request.user, queryset, "adls.view",
            group_home_path="resident__leads__group_home_assignments__group_home",
        )

        if log_date:
            queryset = queryset.filter(log_date=log_date)

        if care_plan_item_uuid:
            queryset = queryset.filter(care_plan_item__uuid=care_plan_item_uuid)

        queryset = queryset.filter(
            deleted_at__isnull=not archived in ["true", "True", "1"]
        )

        serializer = CarePlanDailyLogSerializer(queryset, many=True)

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Daily logs fetched successfully",
                "data": serializer.data,
            }
        )

    @extend_schema(
        summary="Create Daily Log",
        request=CarePlanDailyLogSerializer,
        responses=CarePlanDailyLogSerializer,
        tags=["Residents - Daily Logs"],
    )
    

    def post(self, request):
        """
        Create or update multiple Daily Logs (UPSERT)
        """
        resident_uuid = request.data.get("resident_uuid")
        log_date = request.data.get("log_date")
        shift = request.data.get("shift")
        logs_data = request.data.get("logs", [])

        if not resident_uuid or not log_date or not shift or not logs_data:
            return Response(
                {
                    "status": "error",
                    "code": 400,
                    "message": "resident_uuid, log_date, shift, and logs are required."
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        saved_logs = []



        for log_item in logs_data:
            log_item.update({
                "resident_uuid": resident_uuid,
                "log_date": log_date,
                "shift": shift
            })

            # Try to fetch existing daily log
            try:
                existing_log = CarePlanDailyLog.objects.get(
                    care_plan_item__uuid=log_item["care_plan_item_uuid"],
                    resident__uuid=resident_uuid,
                    log_date=log_date,
                    shift=shift
                )
            except CarePlanDailyLog.DoesNotExist:
                existing_log = None

            # Pass instance if exists for update
            serializer = CarePlanDailyLogSerializer(
                instance=existing_log,
                data=log_item,
                context={"request": request, "created_by": request.user}
            )

            serializer.is_valid(raise_exception=True)
            daily_log = serializer.save()
            saved_logs.append(daily_log)


        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Daily logs saved successfully",
                "data": CarePlanDailyLogSerializer(saved_logs, many=True).data
            },
            status=status.HTTP_200_OK
        )



class CarePlanDailyLogDetailAPIView(APIView):
    """
    Retrieve / Update Daily Log
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "adls.view",
        "PUT": "adls.record",
    }

    def get_object(self, uuid):
        return get_object_or_404(CarePlanDailyLog, uuid=uuid, deleted_at__isnull=True)

    @extend_schema(
        summary="Get Daily Log detail",
        responses=CarePlanDailyLogSerializer,
        tags=["Residents - Daily Logs"],
    )
    def get(self, request, uuid):
        log = self.get_object(uuid)
        return Response(
            {
                "status": "success",
                "code": 200,
                "data": CarePlanDailyLogSerializer(log).data,
            }
        )

    @extend_schema(
        summary="Update Daily Log",
        request=CarePlanDailyLogSerializer,
        responses=CarePlanDailyLogSerializer,
        tags=["Residents - Daily Logs"],
    )
    def put(self, request, uuid):
        log = self.get_object(uuid)

        serializer = CarePlanDailyLogSerializer(log, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        log_audit(
            request=request,
            action="UPDATE",
            message="Daily care plan log updated",
            entity_type="CarePlanDailyLog",
            entity_id=str(log.uuid),
        )

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Daily log updated successfully",
                "data": serializer.data,
            }
        )


class CarePlanDailyLogArchiveAPIView(APIView):
    """
    Archive (soft delete) a Care Plan Daily Log
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "adls.record",
    }

    @extend_schema(
        summary="Archive Daily Log",
        responses={204: None},
        tags=["Residents - Daily Logs"],
    )
    def post(self, request, uuid):
        log = get_object_or_404(CarePlanDailyLog, uuid=uuid, deleted_at__isnull=True)

        log.deleted_at = timezone.now()
        log.save(update_fields=["deleted_at"])

        log_audit(
            request=request,
            action="DELETE",
            message="Daily care plan log archived",
            entity_type="CarePlanDailyLog",
            entity_id=str(log.uuid),
        )

        return Response(
            {
                "status": "success",
                "message": "Daily log archived successfully",
            },
            status=status.HTTP_204_NO_CONTENT,
        )


class CarePlanArchivedDailyLogListAPIView(APIView):
    """
    List Archived (Soft Deleted) Daily Logs
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "adls.view",
    }

    @extend_schema(
        summary="List Archived Daily Logs",
        responses=CarePlanDailyLogSerializer(many=True),
        tags=["Residents - Daily Logs"],
    )
    def get(self, request):
        queryset = CarePlanDailyLog.objects.filter(deleted_at__isnull=False)
        serializer = CarePlanDailyLogSerializer(queryset, many=True)

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Archived daily logs fetched successfully",
                "data": serializer.data,
            }
        )




# ==========================================================
# Care Plan Monthly Report – Create
# ==========================================================
# - Creates one monthly report per resident
# - Stores structured JSON report data
# - generated_by is auto-filled from request.user
# ==========================================================

class CarePlanReportCreateView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "monthly_summary.generate_report",
    }

    @extend_schema(
        summary="Create Monthly Care Plan Report",
        request=CarePlanReportSerializer,
        responses=CarePlanReportSerializer,
        tags=["Residents - Care Plan Reports"],
    )

    def post(self, request, *args, **kwargs):
        serializer = CarePlanReportSerializer(
            data=request.data,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        report = serializer.save()

        # Generate PDF, upload to S3, and link to report
        from residents.services import generate_and_store_report_pdf
        generate_and_store_report_pdf(report, request.user)
        report.refresh_from_db()

        return Response(
            {
                "status": "success",
                "code": 201,
                "message": "Monthly care plan report created successfully",
                "data": CarePlanReportSerializer(report).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ==========================================================
# Care Plan Monthly Report – Retrieve (Resident)
# ==========================================================
# - Fetch monthly report for resident
# - Excludes soft-deleted records
# ==========================================================


class CarePlanReportRetrieveDeleteView(APIView):
    """
    GET  care-plan-reports/<uuid>/ → List reports for resident (uuid = resident's user UUID)
    DELETE care-plan-reports/<uuid>/ → Soft-delete a single report (uuid = report UUID)
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":    "monthly_summary.view",
        "DELETE": "monthly_summary.generate_report",
    }

    @extend_schema(
        summary="List Monthly Care Plan Reports",
        responses=CarePlanReportSerializer(many=True),
        tags=["Residents - Care Plan Reports"],
    )
    def get(self, request, uuid, *args, **kwargs):
        reports = CarePlanReport.objects.filter(
            resident__uuid=uuid,
            deleted_at__isnull=True,
        ).order_by("-report_year", "-report_month")

        paginator = PageNumberPagination()
        paginator.page_size = 10
        paginator.page_size_query_param = "page_size"

        paginated_reports = paginator.paginate_queryset(reports, request)

        if not paginated_reports:
            return Response(
                {
                    "status": "success",
                    "code": 200,
                    "message": "No monthly reports found for this resident",
                    "data": [],
                },
                status=status.HTTP_200_OK,
            )

        serializer = CarePlanReportSerializer(paginated_reports, many=True)

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Monthly care plan reports fetched successfully",
                "data": serializer.data,
                "pagination": {
                    "count": paginator.page.paginator.count,
                    "page": paginator.page.number,
                    "page_size": paginator.page.paginator.per_page,
                    "next": paginator.get_next_link(),
                    "previous": paginator.get_previous_link(),
                },
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Delete Monthly Care Plan Report",
        tags=["Residents - Care Plan Reports"],
    )
    def delete(self, request, uuid, *args, **kwargs):
        try:
            report = CarePlanReport.objects.get(
                uuid=uuid,
                deleted_at__isnull=True,
            )
        except CarePlanReport.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": 404,
                    "message": "Care plan report not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        report.soft_delete()

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Care plan report deleted successfully",
            },
            status=status.HTTP_200_OK,
        )


# ==========================================================
# ResidentScheduledForm – List / Create / Delete
# ==========================================================

class ResidentScheduledFormListCreateAPIView(APIView):
    """
    GET  /api/residents/resident-scheduled-forms/?resident_uuid=<user_uuid>
         Returns all scheduled forms for the given resident (user UUID).

    POST /api/residents/resident-scheduled-forms/
         Create (or upsert) a scheduled form record.
         Body: { user: <user_uuid>, form_name, scheduled_date }
         If a record already exists for (user, form_name) it is updated in-place.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        resident_uuid = request.query_params.get("resident_uuid")
        if not resident_uuid:
            return Response(
                {"status": "error", "message": "resident_uuid is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 1. Try direct user UUID lookup
        qs = ResidentScheduledForm.objects.filter(user__uuid=resident_uuid)

        # 2. Fallback: resident_uuid might be a lead UUID — resolve via lead.user
        if not qs.exists():
            try:
                from leads.models import Lead as LeadModel
                lead = LeadModel.objects.get(uuid=resident_uuid)
                qs = ResidentScheduledForm.objects.filter(user=lead.user)
            except Exception:
                qs = ResidentScheduledForm.objects.none()

        serializer = ResidentScheduledFormSerializer(qs, many=True)

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Resident scheduled forms fetched successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )


    def post(self, request):
        """
        Upsert: if a record for (user, form_name) already exists, touch it.
        Otherwise create a new record.
        """
        user_uuid  = request.data.get("user")
        form_name  = request.data.get("form_name", "").strip()
        scheduled_date = request.data.get("scheduled_date")
        lead_uuid  = request.data.get("lead_uuid")

        if not form_name:
            return Response(
                {
                    "status": "error",
                    "message": "form_name is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user_uuid and not lead_uuid:
            return Response(
                {
                    "status": "error",
                    "message": "user or lead_uuid is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from accounts.models import User as UserModel

        resident_user = None

        # 1. Try resolving via user UUID directly
        if user_uuid:
            try:
                resident_user = UserModel.objects.get(uuid=user_uuid)
            except UserModel.DoesNotExist:
                resident_user = None

        # 2. Fallback: resolve via lead UUID → lead.user
        if resident_user is None and lead_uuid:
            try:
                from leads.models import Lead as LeadModel
                lead = LeadModel.objects.get(uuid=lead_uuid)
                resident_user = lead.user
            except Exception:
                resident_user = None

        # 3. Also try: user_uuid might actually be a lead uuid
        if resident_user is None and user_uuid:
            try:
                from leads.models import Lead as LeadModel
                lead = LeadModel.objects.get(uuid=user_uuid)
                resident_user = lead.user
            except Exception:
                resident_user = None

        if resident_user is None:
            return Response(
                {"status": "error", "message": "User (resident) not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


        # Validate scheduled_date format before hitting the DB
        if scheduled_date:
            from datetime import datetime as _dt
            try:
                _dt.strptime(str(scheduled_date), "%Y-%m-%d")
            except ValueError:
                return Response(
                    {"scheduled_date": ["Date has wrong format. Use one of these formats instead: YYYY-MM-DD."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            # update_or_create ensures we can upate the scheduled_date for existing rows
            defaults_dict = {}
            if scheduled_date:
                defaults_dict["scheduled_date"] = scheduled_date

            obj, created = ResidentScheduledForm.objects.update_or_create(
                user=resident_user,
                form_name=form_name,
                defaults=defaults_dict, 
            )
        except Exception as exc:
            return Response(
                {
                    "status": "error",
                    "message": "An unexpected error occurred. Please try again.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        serializer = ResidentScheduledFormSerializer(obj)
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(
            {
                "status": "success",
                "code": http_status,
                "message": "Scheduled form saved successfully",
                "data": serializer.data,
            },
            status=http_status,
        )


class ResidentScheduledFormDestroyAPIView(APIView):
    """
    DELETE /api/residents/resident-scheduled-forms/<id>/
           Hard-deletes the scheduled form record by primary key.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            obj = ResidentScheduledForm.objects.get(pk=pk)
        except ResidentScheduledForm.DoesNotExist:
            return Response(
                {"status": "error", "message": "Record not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        obj.delete()
        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Scheduled form deleted successfully",
            },
            status=status.HTTP_200_OK,
        )
