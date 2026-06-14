import math
from datetime import datetime
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import HasPermission
from .models import AuditLog
from .serializers import AuditLogListSerializer
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes


class AuditLogListAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "daily_tracking.view",  # Admin, PD, PM only
    }

    @extend_schema(
        operation_id="list_audit_logs",
        summary="List audit logs",
        description="Retrieve paginated audit logs with filtering",
        tags=["Audit Logs"],
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, default=1),
            OpenApiParameter("size", OpenApiTypes.INT, OpenApiParameter.QUERY, default=10),
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("entity_type", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("action", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter(
                "date",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="YYYY-MM-DD or MM/DD/YYYY",
            ),
            OpenApiParameter(
                "group_home",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Group Home UUID",
            ),
            OpenApiParameter(
                "target_user",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Filter by target user UUID",
            ),
        ],
    )
    def get(self, request):
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))
        search = request.query_params.get("search")
        entity_type = request.query_params.get("entity_type")
        action = request.query_params.get("action")
        date_filter = request.query_params.get("date")
        group_home_uuid = request.query_params.get("group_home")

        target_user_uuid = request.query_params.get("target_user")

        queryset = AuditLog.objects.select_related(
            "user", "user__role", "group_home", "target_user"
        ).order_by("-created_at")

        if entity_type:
            # "Document" → match all document subtypes ending with "Document"
            #   e.g. "Lead/Document", "Resident/Document", "GroupHome/Document", "Document"
            # Composite types like "Lead/Document" → exact match
            # Simple types like "Lead", "Appointment" → exact match to avoid matching "Lead/Document"
            if entity_type == "Document":
                queryset = queryset.filter(entity_type__iendswith="Document")
            else:
                queryset = queryset.filter(entity_type__iexact=entity_type)

        if action:
            queryset = queryset.filter(action=action)

        if group_home_uuid:
            queryset = queryset.filter(group_home__uuid=group_home_uuid)

        if target_user_uuid:
            queryset = queryset.filter(target_user__uuid=target_user_uuid)

        if date_filter:
            try:
                parsed_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            except ValueError:
                parsed_date = datetime.strptime(date_filter, "%m/%d/%Y").date()

            queryset = queryset.filter(created_at__date=parsed_date)

        if search:
            terms = search.split()
            q = Q()
            for term in terms:
                q &= (
                    Q(message__icontains=term)
                    | Q(entity_type__icontains=term)
                    | Q(user__email__icontains=term)
                    | Q(user__first_name__icontains=term)
                    | Q(user__last_name__icontains=term)
                    | Q(target_user_name__icontains=term)
                    | Q(target_user__first_name__icontains=term)
                    | Q(target_user__last_name__icontains=term)
                )

            queryset = queryset.filter(q)

        # Explicit count on the FILTERED queryset (same WHERE conditions)
        total_count = queryset.count()

        # Manual pagination using the same filtered queryset
        total_pages = math.ceil(total_count / size) if total_count > 0 else 1

        # Clamp page to valid range
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages

        start = (page - 1) * size
        end = start + size
        page_results = queryset[start:end]

        serializer = AuditLogListSerializer(page_results, many=True)

        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Audit logs fetched successfully",
                "data": {
                    "results": serializer.data,
                    "pagination": {
                        "page": page,
                        "size": size,
                        "total_pages": total_pages,
                        "total_records": total_count,
                        "has_next": page < total_pages,
                        "has_previous": page > 1,
                    },
                },
            },
            status=status.HTTP_200_OK,
        )
