"""
providers/views.py
------------------
REST API views for the Provider model.

Endpoints:
  GET  /api/providers/              → list all providers (filterable by lead_uuid)
  POST /api/providers/              → create a new provider
  GET  /api/providers/<uuid>/       → retrieve a single provider
  PUT  /api/providers/<uuid>/       → update a provider (partial)
  DELETE /api/providers/<uuid>/     → soft-delete a provider
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.utils import timezone
from django.core.paginator import Paginator

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from .models import Provider
from .serializers import ProviderSerializer, ProviderListSerializer
from accounts.permissions import HasPermission
from audit_logs.utils import log_action


# ============================================================
# ProviderAPIView  —  GET (list) + POST (create)
# ============================================================

class ProviderAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "providers.view",
        "POST": "providers.create",
    }

    # ------------------------------------------------------------------ GET
    @extend_schema(
        operation_id="list_providers",
        summary="List providers",
        description=(
            "Retrieve a paginated list of providers. "
            "Optionally filter by resident (lead) using `lead_uuid`."
        ),
        tags=["Providers"],
        parameters=[
            OpenApiParameter(
                name="lead_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter providers by resident (lead) UUID",
                required=False,
            ),
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Search by provider name or specialty",
                required=False,
            ),
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page number (default: 1)",
                required=False,
            ),
            OpenApiParameter(
                name="size",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Records per page (default: 10)",
                required=False,
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))

        providers = (
            Provider.objects
            .select_related("lead", "created_by")
            .filter(deleted_at__isnull=True)
            .order_by("-created_at")
        )

        # ---- FILTER: lead_uuid ----
        lead_uuid = request.query_params.get("lead_uuid")
        if lead_uuid:
            providers = providers.filter(lead__uuid=lead_uuid)

        # ---- SEARCH: name / specialty ----
        search = request.query_params.get("search", "").strip()
        if search:
            from django.db.models import Q
            providers = providers.filter(
                Q(name__icontains=search) |
                Q(specialty__icontains=search)
            )

        paginator = Paginator(providers, size)
        page_obj = paginator.get_page(page)

        serializer = ProviderListSerializer(page_obj, many=True)

        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Providers fetched successfully",
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
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------------------ POST
    @extend_schema(
        operation_id="create_provider",
        summary="Create provider",
        description="Create a new provider. Pass `lead_uuid` to link to a resident.",
        request=ProviderSerializer,
        tags=["Providers"],
        responses={
            201: ProviderSerializer,
            400: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        serializer = ProviderSerializer(
            data=request.data,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                provider = serializer.save(
                    created_by=request.user if request.user.is_authenticated else None
                )

                log_action(
                    request=request,
                    action="CREATE",
                    entity_type="Provider",
                    entity_id=str(provider.uuid),
                    message=f"Provider '{provider.name}' created",
                )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_201_CREATED,
                    "message": "Provider created successfully",
                    "data": ProviderSerializer(provider, context={"request": request}).data,
                },
                status=status.HTTP_201_CREATED,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to create provider",
                    "errors": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ============================================================
# ProviderDetailAPIView  —  GET + PUT + DELETE (by UUID)
# ============================================================

class ProviderDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":    "providers.view",
        "PUT":    "providers.edit",
        "DELETE": "providers.delete",
    }

    def _get_provider(self, uuid):
        """Shared lookup helper — returns provider or None."""
        return (
            Provider.objects
            .select_related("lead", "created_by")
            .filter(uuid=uuid, deleted_at__isnull=True)
            .first()
        )

    # ------------------------------------------------------------------ GET
    @extend_schema(
        operation_id="get_provider",
        summary="Get provider details",
        description="Retrieve a single provider by UUID.",
        tags=["Providers"],
        responses={
            200: ProviderSerializer,
            404: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request, uuid):
        provider = self._get_provider(uuid)

        if not provider:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Provider not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ProviderSerializer(provider, context={"request": request})
        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Provider fetched successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------------------ PUT
    @extend_schema(
        operation_id="update_provider",
        summary="Update provider",
        description="Partially update a provider by UUID.",
        request=ProviderSerializer,
        tags=["Providers"],
        responses={
            200: ProviderSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
    )
    def put(self, request, uuid):
        provider = self._get_provider(uuid)

        if not provider:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Provider not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ProviderSerializer(
            provider,
            data=request.data,
            partial=True,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                updated_provider = serializer.save()

            log_action(
                request=request,
                action="UPDATE",
                entity_type="Provider",
                entity_id=str(updated_provider.uuid),
                message=f"Provider '{updated_provider.name}' updated",
            )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Provider updated successfully",
                    "data": ProviderSerializer(
                        updated_provider, context={"request": request}
                    ).data,
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to update provider",
                    "errors": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------ DELETE (soft)
    @extend_schema(
        operation_id="delete_provider",
        summary="Delete provider",
        description="Soft-delete a provider by UUID.",
        tags=["Providers"],
        responses={
            200: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
    )
    def delete(self, request, uuid):
        provider = self._get_provider(uuid)

        if not provider:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Provider not found",
                    "errors": {"uuid": str(uuid)},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            with transaction.atomic():
                provider.deleted_at = timezone.now()
                provider.save(update_fields=["deleted_at", "updated_at"])

            log_action(
                request=request,
                action="DELETE",
                entity_type="Provider",
                entity_id=str(provider.uuid),
                message=f"Provider '{provider.name}' deleted",
            )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Provider deleted successfully",
                    "data": None,
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to delete provider",
                    "errors": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
