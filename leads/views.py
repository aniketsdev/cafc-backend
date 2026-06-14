import json
import logging
from django.shortcuts import render
from rest_framework.views import APIView
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from accounts.models import PasswordResetToken
from django.http import Http404
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from rest_framework import serializers, status
from accounts.email_service import send_email
from django.core.paginator import Paginator
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.core.exceptions import ValidationError
from leads.models import Lead, Insurance
from leads.serializers import (
    LeadCreateSerializer,
    LeadDetailSerializer,
    LeadSerializer,
    InsuranceSerializer,
)
from leads.models import Lead
from accounts.models import User, Address, Role
from accounts.serializers import UserSerializer, AddressSerializer
from accounts.utils import generate_random_password, generate_uuid
from audit_logs.utils import log_action, get_active_group_home
from django.contrib.auth.hashers import make_password
from django.db.models import Q, Value, CharField
from django.db.models.functions import Cast, Concat, Upper
from django.shortcuts import get_object_or_404
from django.http import Http404
from rest_framework.exceptions import APIException
from leads.models import Lead, LeadGroupHomeAssignment
from leads.serializers import CompleteOnboardingSerializer,MoveOutSerializer,ReAdmitSerializer,ResidentListSerializer,LeadDocumentSerializer
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from accounts.permissions import scope_queryset, check_scope_access, HasPermission, has_permission
from leads.serializers import RejectReferralSerializer
from leads.serializers import TransferResidentSerializer
from media.hooks import upload_media, update_media
from django.contrib.contenttypes.models import ContentType
from media.models import Media
from leads.status import compute_lead_status, recompute_and_save

logger = logging.getLogger(__name__)


class DuplicateEmailError(Exception):
    """Raised inside transaction.atomic() when a guardian/agent email already exists."""
    def __init__(self, message, field):
        self.message = message
        self.field = field
        super().__init__(message)


def generate_set_password_link(user):
    token_obj = PasswordResetToken.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(minutes=30)  # ✅ REQUIRED
    )

    return f"{settings.FRONTEND_BASE_URL}/set-password?token={token_obj.token}"


def _compute_lead_status(has_demographics, has_insurance, documents_checklist_complete):
    """Legacy wrapper kept for backward compatibility during create/update.
    The authoritative logic now lives in leads.status.compute_lead_status().
    This is only used during initial create when the lead object doesn't exist yet.
    """
    if not (has_demographics and has_insurance):
        return "DRAFT"
    if documents_checklist_complete:
        return "UNDER_REVIEW"
    return "DOCS_PENDING"


#------------------LeadCreateView----------------------------------------------------------
class LeadAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "leads.view",
        "POST": "leads.create",
    }
    @extend_schema(
        operation_id="create_lead",
        summary="Create a new lead",
        description="Create a new lead along with associated user, address, and insurance details.",
        request=LeadCreateSerializer,
        tags=["Leads"]
    )

    def post(self, request):
        # Support multipart/form-data: if avatar file is sent, lead payload is in request.data['data'] as JSON string
        avatar_file = request.FILES.get("avatar")
        if avatar_file and request.data.get("data"):
            try:
                request_data = json.loads(request.data.get("data"))
            except (TypeError, ValueError):
                request_data = request.data
        else:
            request_data = request.data

        serializer = LeadCreateSerializer(data=request_data)
      
        if not serializer.is_valid():
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                data = serializer.validated_data
                email = data.pop("email", None) 
                # ---------------- EMAIL UNIQUENESS CHECK ----------------
                

                if email:
                    email = email.lower()

                    if User.objects.filter(email=email).exists():
                        raise DuplicateEmailError("Email already exists.", "email")

                
                guardian_data = data.pop("guardian", None)
                guardian_uuid = data.pop("guardian_uuid", None)
                agent_data = data.pop("agent", None)
                agent_uuid = data.pop("agent_uuid", None)
                address_data = data.pop("address", None)
                insurance_data = data.pop("insurance", None)

                # ----------------------------------------------------
                # Lead email must be unique
                # ----------------------------------------------------

                lead_role = Role.objects.get(type="LEAD")

                # if not email:
                #  email = f"{generate_uuid()}@temp.local"

                lead_user = User.objects.create(
                    uuid=generate_uuid(),
                    username=email or generate_uuid() ,         # always non-null now
                    first_name=data.pop("first_name"),
                    last_name=data.pop("last_name"),
                    email=email if email else None,             # always non-null now
                    phone=data.pop("phone", None),
                    role=lead_role,
                    password=None,
                    active=True,
                )

                 # ---------------- CREATE GUARDIAN USER ----------------
                guardian_user = None

                # 1️⃣ guardian_uuid has highest priority
                if guardian_uuid:
                    guardian_user = User.objects.filter(
                        uuid=guardian_uuid,
                        role__type="GUARDIAN"
                    ).first()

                    if not guardian_user:
                        raise serializers.ValidationError(
                            {"guardian_uuid": "Invalid guardian_uuid"}
                        )

                # 2️⃣ guardian object
                elif guardian_data:
                    guardian_email = guardian_data.get("email")

                    if guardian_email:
                        guardian_email = guardian_email.lower()

                    guardian_role = Role.objects.get(type="GUARDIAN")

                    if User.objects.filter(email=guardian_email).exists():
                        raise DuplicateEmailError(
                            "Email already exists",
                            "guardian_email",
                        )

                    guardian_user = User.objects.create(
                        uuid=generate_uuid(),
                        username=guardian_email,
                        first_name=guardian_data["first_name"],
                        last_name=guardian_data.get("last_name", ""),
                        email=guardian_email,
                        phone=guardian_data.get("phone"),
                        role=guardian_role,
                        active=True,
                    )

                    # ✅ Send email ONLY for new guardian
                    link = generate_set_password_link(guardian_user)
                    guardian_full_name = guardian_user.first_name
                    if guardian_user.last_name:
                        guardian_full_name = f"{guardian_user.first_name} {guardian_user.last_name}"
                    # Use default argument to capture values correctly in lambda
                    transaction.on_commit(
                        lambda name=guardian_full_name, email=guardian_user.email, link=link: send_email(
                            to_email=email,
                            subject="Guardian Account Created",
                            template_name="guardian_credentials.html",
                            context={
                                "name": name,
                                "email": email,
                                "role": "Guardian",
                                "set_password_link": link,
                            },
                        )
                    )

                  # ==================================================
                # AGENT FLOW
                # ==================================================
                # ---------------- AGENT FLOW ----------------
              
                agent_user = None

                # 1️⃣ agent_uuid has highest priority
                if agent_uuid:
                    agent_user = User.objects.filter(
                        uuid=agent_uuid,
                        role__type="AGENT"
                    ).first()

                    if not agent_user:
                        raise serializers.ValidationError(
                            {"agent_uuid": "Invalid agent_uuid"}
                        )

                # 2️⃣ agent object
                elif agent_data:
                    agent_email = agent_data.get("email")

                    if agent_email:
                        agent_email = agent_email.lower()

                    agent_role = Role.objects.get(type="AGENT")

                    if User.objects.filter(email=agent_email).exists():
                        raise DuplicateEmailError(
                            "Email already exists",
                            "agent_email",
                        )

                    agent_user = User.objects.create(
                        uuid=generate_uuid(),
                        username=agent_email,
                        first_name=agent_data["first_name"],
                        last_name=agent_data.get("last_name", ""),
                        email=agent_email,
                        phone=agent_data.get("phone"),
                        role=agent_role,
                        active=True,
                    )

                    # ✅ LOG: Agent Created
                    log_action(
                        request=request,
                        action="CREATE",
                        entity_type="Agent",
                        entity_id=str(agent_user.uuid),
                        message="Agent account created",
                    )
                    # ✅ Send email ONLY for new agent
                    link = generate_set_password_link(agent_user)
                    agent_full_name = agent_user.first_name
                    if agent_user.last_name:
                        agent_full_name = f"{agent_user.first_name} {agent_user.last_name}"
                    # Use default argument to capture values correctly in lambda
                    transaction.on_commit(
                        lambda name=agent_full_name, email=agent_user.email, link=link: send_email(
                            to_email=email,
                            subject="Area Agency Account Created",
                            template_name="agent_credentials.html",
                            context={
                                "name": name,
                                "email": email,
                                "role": "Area Agency",
                                "set_password_link": link,
                            },
                        )
                    )
                # ---------------- CREATE ADDRESS ----------------
                
                address = None

                if address_data:
                    address = Address.objects.create(
                        uuid=generate_uuid(),
                        **address_data
                    )

                # ---------------- COMPUTE LEAD STATUS (backend-owned) ----------------
                data.pop("status", None)  # ignore client-sent status
                documents_checklist_complete = data.pop("documents_checklist_complete", False)
                has_demographics = bool(guardian_uuid or guardian_data or agent_uuid or agent_data)
                has_insurance = bool(
                    insurance_data
                    and (insurance_data.get("provider") or insurance_data.get("policy_number"))
                )
                lead_status = _compute_lead_status(
                    has_demographics, has_insurance, documents_checklist_complete
                )

                # ---------------- CREATE LEAD ----------------
                lead = Lead.objects.create(
                    uuid=generate_uuid(),
                    user=lead_user,
                    guardian=guardian_user,
                    agent=agent_user,
                    address=address,
                    status=lead_status,
                    **data
                )
                log_action(
                    request=request,
                    action="CREATE",
                    entity_type="Lead",
                    entity_id=str(lead.uuid),
                    message="Lead account created",
                )

                # ---------------- CREATE INSURANCE ----------------
                insurance = None
                if insurance_data:
                    insurance = Insurance.objects.create(
                        user=lead_user,
                        **insurance_data
                    )

            # ---------------- AVATAR UPLOAD (after create) ----------------
            if avatar_file:
                media, error = upload_media(
                    file=avatar_file,
                    user=lead_user,
                    content_type_app="accounts",
                    content_type_model="User",
                    object_id=lead_user.id,
                    alt_text=f"{lead_user.first_name} {lead_user.last_name} profile picture",
                )
                if not error and hasattr(lead_user, "profile_picture_id"):
                    lead_user.profile_picture_id = media.id
                    lead_user.save(update_fields=["profile_picture_id"])

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_201_CREATED,
                    "message": "Lead created successfully",
                    "data": {
                        "user": UserSerializer(lead_user).data,
                        "lead": LeadSerializer(lead).data,
                        "guardian": UserSerializer(guardian_user).data if guardian_user else None,
                        "agent": UserSerializer(agent_user).data if agent_user else None,
                        "address": AddressSerializer(address).data if address else None,
                        "insurance": InsuranceSerializer(insurance).data
                        if insurance else None,
                    },
                },
                status=status.HTTP_201_CREATED,
            )

        except DuplicateEmailError as e:
            return Response(
                {
                    "status": "error",
                    "code": 400,
                    "message": e.message,
                    "field": e.field,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except serializers.ValidationError as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "errors": e.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            logger.error("Lead creation failed", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

#----------------LeadListAPIView----------------------------------------------        
    @extend_schema(
        operation_id="list_leads",
        summary="List all leads",
        description="Retrieve a paginated list of all leads with filtering and search support.",
        tags=["Leads"],
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
                description="Search by user name or referral source",
                required=False,
            ),
            OpenApiParameter(
                name="status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter leads by status",
                required=False,
            ),
            OpenApiParameter(
                name="referral_source",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter leads by referral source",
                required=False,
            ),
        ],
    )
    
    def get(self, request):
        try:
            page = int(request.query_params.get("page", 1))
            size = int(request.query_params.get("size", 10))

            raw_search = request.query_params.get("search")
            search = (raw_search or "").strip()

            # Get filters from query params
            status_filter = request.query_params.get("status")
            referral_source_filter = request.query_params.get("referral_source")

            leads_qs = (
                Lead.objects
                .select_related("user", "guardian", "address")
                .order_by("-created_at")
            )

            # Scope to assigned group home for ASSIGNED_HOME users. Exclude
            # residents who have MOVED_OUT of the home — they no longer belong
            # to it, so home-scoped roles (Coordinator/DSP/Nurse) must not see
            # them. ALL-scope roles (Admin/PM) bypass scope_queryset and still
            # see moved-out for oversight.
            leads_qs = scope_queryset(
                request.user, leads_qs, "leads.view",
                group_home_path="group_home_assignments__group_home",
                assignment_status_path="group_home_assignments__status",
            )

            # Apply filtering if filters provided
            if status_filter:
                leads_qs = leads_qs.filter(status=status_filter)

            if referral_source_filter:
                leads_qs = leads_qs.filter(referral_source=referral_source_filter)


             # ---------- SEARCH ----------

            if search.lower() not in {"", "null", "undefined", "none"}:
                leads_qs = leads_qs.annotate(
                    full_name=Concat(
                        "user__first_name",
                        Value(" "),
                        "user__last_name",
                        output_field=CharField()
                    )
                ).filter(
                    Q(user__first_name__icontains=search) |
                    Q(user__last_name__icontains=search) |
                    Q(full_name__icontains=search) |
                    Q(referral_source__icontains=search)
                )


            paginator = Paginator(leads_qs, size)
            page_obj = paginator.get_page(page)

            # Prefetch avatar URLs for lead users to avoid N+1
            avatar_urls_map = {}
            users = [lead.user for lead in page_obj if getattr(lead, "user", None)]
            profile_picture_ids = [
                str(u.profile_picture_id) for u in users
                if getattr(u, "profile_picture_id", None)
            ]
            if profile_picture_ids:
                medias = Media.objects.filter(id__in=profile_picture_ids, status="active")
                media_id_to_url = {str(m.id): m.get_file_url() for m in medias}
                avatar_urls_map = {
                    u.id: media_id_to_url.get(str(u.profile_picture_id))
                    for u in users
                    if getattr(u, "profile_picture_id", None)
                }
            serializer_context = {"avatar_urls": avatar_urls_map}
            serializer = LeadDetailSerializer(page_obj, many=True, context=serializer_context)

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Leads fetched successfully",
                    "data": {
                        "results": serializer.data,
                        "pagination": {
                            "page": page,
                            "size": size,
                            "total_pages": paginator.num_pages,
                            "total_records": paginator.count,
                        },
                    },
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.error("Failed to fetch leads", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
    
    
#-------------LeadDetailUpdateAPIView----------------------------------------------
# SINGLE LEAD → GET + UPDATE + STATUS UPDATE

class LeadDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PUT":   "leads.edit",   # matches seed: {"module": "leads", "key": "edit"}
        "PATCH": "leads.edit",   # matches seed: {"module": "leads", "key": "edit"}
    }
    # GET: allow users with leads.view (PD/PM/BCBA) OR adls.view (DSP/Nurse)
    # PUT/PATCH remain governed by leads.edit via required_permission above.
    allow_any_permissions = {
        "GET": ["leads.view", "adls.view"],
    }
    # Guardian/Agent can GET their resident's lead detail (read-only portal access).
    # PUT/PATCH remain staff-only.
    allow_portal_roles = {"GET"}

    @extend_schema(
        operation_id="get_lead_detail",
        summary="Get lead details",
        description="Fetch detailed information of a lead including user, address, and insurance by lead UUID.",
        tags=["Leads"]
    )
    def get(self, request, uuid):
        try:
            lead = get_object_or_404(
                Lead.objects.select_related("user", "guardian", "address"),
                uuid=uuid,
            )

            # Scope check: ASSIGNED_HOME users can only view leads in their home.
            # Use leads.view scope when available; fall back to adls.view for
            # roles like DSP/Nurse that have adls.view (ASSIGNED_HOME) but not leads.view.
            scope_perm = "leads.view" if has_permission(request.user, "leads.view") else "adls.view"
            if not check_scope_access(
                request.user, lead, scope_perm,
                group_home_path="group_home_assignments__group_home",
                assignment_status_path="group_home_assignments__status",
            ):
                return Response(
                    {"status": "error", "code": status.HTTP_404_NOT_FOUND, "message": "Lead not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            return Response(
                    {
                        "status": "success",
                        "code": status.HTTP_200_OK,
                        "message": "Lead fetched successfully",
                        "data": LeadDetailSerializer(lead).data,
                    },
                    status=status.HTTP_200_OK,
                )

        except Lead.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Lead not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except ValidationError:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Invalid UUID format",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    # -------------------- UPDATE LEAD (PUT) --------------------------------

    @extend_schema(
        operation_id="update_lead",
        summary="Update lead details",
        description="Update a lead and its associated user, address, and insurance information using the lead UUID.",
        request=LeadSerializer,  
        tags=["Leads"]
    )
    def put(self, request, uuid):
        try:
            lead = Lead.objects.select_related(
                "user", "guardian", "agent", "address"
            ).get(uuid=uuid)

            # Scope check: ASSIGNED_HOME users can only edit leads in their home
            if not check_scope_access(
                request.user, lead, "leads.edit",
                group_home_path="group_home_assignments__group_home",
            ):
                return Response(
                    {"status": "error", "code": status.HTTP_404_NOT_FOUND, "message": "Lead not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Support multipart: if avatar is sent, payload is in request.data['data'] as JSON string
            avatar_file = request.FILES.get("avatar")
            if avatar_file and request.data.get("data"):
                try:
                    data = json.loads(request.data.get("data"))
                except (TypeError, ValueError):
                    data = request.data.copy()
            else:
                data = request.data.copy()

            new_guardian_created = False
            new_agent_created = False


            with transaction.atomic():

                # ================= USER =================
                # Support both nested {"user": {...}} and flat {"first_name": ..., "last_name": ...} payloads
                user_data = data.pop("user", None)
                if not user_data:
                    # Extract top-level user fields (matches the POST/create payload format)
                    flat_user_fields = {}
                    for field in ("first_name", "last_name", "phone"):
                        if field in data:
                            flat_user_fields[field] = data.pop(field)
                    # Remove email from data but don't allow updating it (same as nested flow)
                    data.pop("email", None)
                    if flat_user_fields:
                        user_data = flat_user_fields

                if user_data:
                    user_data.pop("email", None)
                    user_data.pop("password", None)

                    serializer = UserSerializer(
                        lead.user,
                        data=user_data,
                        partial=True
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

                # ================= GUARDIAN =================

                guardian_data = data.pop("guardian", None)
                # Distinguish between "not sent" and "sent as null"
                guardian_uuid_sent = "guardian_uuid" in data
                guardian_uuid = data.pop("guardian_uuid", None)
                guardian_user = lead.guardian

                if guardian_uuid_sent and guardian_uuid is None:
                    # Explicitly sent as null — clear the guardian assignment
                    guardian_user = None

                elif guardian_uuid:
                    guardian_user = User.objects.filter(
                        uuid=guardian_uuid,
                        role__type="GUARDIAN"
                    ).first()

                    if not guardian_user:
                        raise serializers.ValidationError({"guardian_uuid": "Invalid guardian_uuid"})

                elif guardian_data:
                    guardian_email = guardian_data.get("email")
                    if guardian_email:
                        guardian_email = guardian_email.lower()

                    guardian_role = Role.objects.get(type="GUARDIAN")

                    new_guardian_created = False

                    if User.objects.filter(email=guardian_email).exists():
                        raise DuplicateEmailError(
                            "Email already exists",
                            "guardian_email",
                        )

                    guardian_user = User.objects.create(
                        uuid=generate_uuid(),
                        username=guardian_email,
                        first_name=guardian_data["first_name"],
                        last_name=guardian_data.get("last_name", ""),
                        email=guardian_email,
                        phone=guardian_data.get("phone"),
                        role=guardian_role,
                        active=True,
                    )
                    new_guardian_created = True


                # ================= AGENT =================
                agent_data = data.pop("agent", None)
                # Distinguish between "not sent" and "sent as null"
                agent_uuid_sent = "agent_uuid" in data
                agent_uuid = data.pop("agent_uuid", None)
                agent_user = lead.agent

                # 1️⃣ agent_uuid explicitly null — clear the agent assignment
                if agent_uuid_sent and agent_uuid is None:
                    agent_user = None

                # 2️⃣ agent_uuid provided — look up user
                elif agent_uuid:
                    agent_user = User.objects.filter(
                        uuid=agent_uuid,
                        role__type="AGENT"
                    ).first()

                    if not agent_user:
                        raise serializers.ValidationError({"agent_uuid": "Invalid agent_uuid"})

                # 3️⃣ agent object
                elif agent_data:
                    agent_email = agent_data.get("email")
                    if agent_email:
                        agent_email = agent_email.lower()

                    agent_role = Role.objects.get(type="AGENT")

                    # 🔑 CREATE if missing
                    new_agent_created = False

                    if User.objects.filter(email=agent_email).exists():
                        raise DuplicateEmailError(
                            "Email already exists",
                            "agent_email",
                        )

                    agent_user = User.objects.create(
                        uuid=generate_uuid(),
                        username=agent_email,
                        first_name=agent_data["first_name"],
                        last_name=agent_data.get("last_name", ""),
                        email=agent_email,
                        phone=agent_data.get("phone"),
                        role=agent_role,
                        active=True,
                    )
                    new_agent_created = True

                # ================= ADDRESS =================
                address_data = data.pop("address", None)
                if address_data and lead.address:
                    serializer = AddressSerializer(
                        lead.address,
                        data=address_data,
                        partial=True
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

                # ================= INSURANCE =================
                insurance_data = data.pop("insurance", None)

                if insurance_data:
                    insurance = Insurance.objects.filter(
                        user=lead.user,
                        deleted_at__isnull=True
                    ).first()

                    # CREATE if missing
                    if not insurance:
                        insurance = Insurance.objects.create(
                            user=lead.user,
                            **insurance_data
                        )

                    # UPDATE if exists
                    else:
                        for field, value in insurance_data.items():
                            setattr(insurance, field, value)

                        insurance.save()

                # ================= LEAD =================
                data.pop("status", None)  # ignore client-sent status; backend computes
                documents_checklist_complete = data.pop("documents_checklist_complete", False)

                serializer = LeadSerializer(
                    lead,
                    data=data,
                    partial=True
                )
                serializer.is_valid(raise_exception=True)
                serializer.save(
                    guardian=guardian_user,
                    agent=agent_user
                )

                # Recompute status using centralized logic (server-side checks)
                lead.refresh_from_db()
                recompute_and_save(lead, request=request)

                log_action(
                    request=request,
                    action="UPDATE",
                    entity_type="Lead",
                    entity_id=str(lead.uuid),
                    message="Lead updated successfully",
                )

            # ---------------- SEND GUARDIAN EMAIL IF NEW ----------------
            if guardian_data and new_guardian_created:
                link = generate_set_password_link(guardian_user)

                guardian_full_name = guardian_user.first_name
                if guardian_user.last_name:
                    guardian_full_name = f"{guardian_user.first_name} {guardian_user.last_name}"

                transaction.on_commit(
                    lambda name=guardian_full_name,
                        email=guardian_user.email,
                        link=link: send_email(
                        to_email=email,
                        subject="Guardian Account Created",
                        template_name="guardian_credentials.html",
                        context={
                            "name": name,
                            "email": email,
                            "role": "Guardian",
                            "set_password_link": link,
                        },
                    )
                )

            # ---------------- SEND AGENT EMAIL IF NEW ----------------
            if agent_data and new_agent_created:
                link = generate_set_password_link(agent_user)

                agent_full_name = agent_user.first_name
                if agent_user.last_name:
                    agent_full_name = f"{agent_user.first_name} {agent_user.last_name}"

                transaction.on_commit(
                    lambda name=agent_full_name,
                        email=agent_user.email,
                        link=link: send_email(
                        to_email=email,
                        subject="Area Agency Account Created",
                        template_name="agent_credentials.html",
                        context={
                            "name": name,
                            "email": email,
                            "role": "Area Agency",
                            "set_password_link": link,
                        },
                    )
                )

            # ---------------- AVATAR REMOVAL (lead's user) ----------------
            # When frontend sends avatar_url=null + profile_picture_media_id=null, clear the avatar
            if not avatar_file and lead.user_id:
                json_data = data  # data is already parsed (dict)
                avatar_url_sent = 'avatar_url' in json_data or 'profile_picture_media_id' in json_data
                avatar_url_val = json_data.get('avatar_url')
                pic_media_val = json_data.get('profile_picture_media_id')
                is_null_removal = (
                    avatar_url_sent and
                    avatar_url_val is None and
                    pic_media_val is None
                )
                if is_null_removal:
                    try:
                        user = lead.user
                        ct_user = ContentType.objects.get(app_label='accounts', model='user')
                        Media.objects.filter(
                            content_type=ct_user,
                            object_id=user.id,
                            file_type='image',
                            status='active',
                        ).exclude(alt_text__icontains='signature').update(status='deleted')
                        if hasattr(user, 'profile_picture_id'):
                            user.profile_picture_id = None
                            user.save(update_fields=['profile_picture_id'])
                    except Exception as _e:
                        logger.warning("Lead avatar clear failed: %s", _e)

            # ---------------- AVATAR UPLOAD (lead's user) ----------------
            if avatar_file and lead.user_id:
                user = lead.user
                upload_user = (
                    request.user
                    if hasattr(request.user, "id") and request.user.id
                    else user
                )
                existing_avatar_id = getattr(user, "profile_picture_id", None)
                if not existing_avatar_id:
                    try:
                        content_type_user = ContentType.objects.get(
                            app_label="accounts", model="user"
                        )
                        existing_avatar = Media.objects.filter(
                            content_type=content_type_user,
                            object_id=user.id,
                            file_type="image",
                            status="active",
                        ).order_by("-uploaded_at").first()
                        if existing_avatar:
                            existing_avatar_id = str(existing_avatar.id)
                    except (ContentType.DoesNotExist, Exception):
                        existing_avatar_id = None
                if existing_avatar_id:
                    media, error = update_media(
                        media_id=existing_avatar_id,
                        file=avatar_file,
                        user=upload_user,
                        alt_text=f"{user.first_name} {user.last_name} profile picture",
                    )
                    if error:
                        return Response(
                            {
                                "status": "error",
                                "code": status.HTTP_400_BAD_REQUEST,
                                "message": f"Failed to update avatar: {error}",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                else:
                    media, error = upload_media(
                        file=avatar_file,
                        user=upload_user,
                        content_type_app="accounts",
                        content_type_model="User",
                        object_id=user.id,
                        alt_text=f"{user.first_name} {user.last_name} profile picture",
                    )
                    if error:
                        return Response(
                            {
                                "status": "error",
                                "code": status.HTTP_400_BAD_REQUEST,
                                "message": f"Failed to upload avatar: {error}",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    if hasattr(user, "profile_picture_id"):
                        user.profile_picture_id = media.id
                        user.save(update_fields=["profile_picture_id"])
                lead.refresh_from_db()

            # ✅ RETURN MUST BE OUTSIDE `with`
            lead.refresh_from_db()
            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Lead updated successfully",
                    "data": LeadDetailSerializer(lead).data,
                },
                status=status.HTTP_200_OK,
            )

        except Lead.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Lead not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except DuplicateEmailError as e:
            return Response(
                {
                    "status": "error",
                    "code": 400,
                    "message": e.message,
                    "field": e.field,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except serializers.ValidationError as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "errors": e.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            logger.error("Lead update failed", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

#-------------LeadUpdateStatusView-----------------------------------------------
class LeadRefreshStatusAPIView(APIView):
    """Recalculate lead status based on current data (server-side).
    Status is fully backend-owned; admin cannot override manually."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="refresh_lead_status",
        summary="Refresh lead status",
        description=(
            "Recalculate lead status from current demographics, insurance, "
            "document checklist, and consent form data. "
            "Status is backend-owned and cannot be set manually."
        ),
        responses={
            200: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
        tags=["Leads"],
    )
    def post(self, request, uuid):
        try:
            lead = Lead.objects.get(uuid=uuid)
        except Lead.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Lead not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        changed, old_status, new_status = recompute_and_save(lead, request=request)

        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Status refreshed" if changed else "Status unchanged",
                "data": {
                    "uuid": str(lead.uuid),
                    "old_status": old_status,
                    "status": new_status,
                    "changed": changed,
                },
            },
            status=status.HTTP_200_OK,
        )
        
#-------------CompleteOnboardingAPIView-----------------------------

class CompleteOnboardingAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "onboarding.complete",
    }

    @extend_schema(
    operation_id="complete_onboarding",
    summary="Complete lead onboarding",
    description=(
        "Assign a lead to a group home and room, close any previous "
        "active assignments, and mark the lead onboarding as completed."
    ),
    request=CompleteOnboardingSerializer,
    responses={200: OpenApiTypes.OBJECT},
    tags=["Lead Assignments"],
    )
    
    def post(self, request, lead_uuid):
        try:
            lead = get_object_or_404(Lead, uuid=lead_uuid)
            user = lead.user

            # Validate: only allow completion from ONBOARDING_IN_PROGRESS or UNDER_REVIEW
            allowed_statuses = ("ONBOARDING_IN_PROGRESS", "UNDER_REVIEW", "DOCS_PENDING")
            if lead.status not in allowed_statuses:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": (
                            f"Cannot complete onboarding. Current status is '{lead.status}'. "
                            f"Lead must be in one of: {', '.join(allowed_statuses)}."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            serializer = CompleteOnboardingSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data

            with transaction.atomic():

                # 🔒 STEP 1: Check for existing assignment
                existing_assignment = (
                    LeadGroupHomeAssignment.objects
                    .filter(lead=lead)
                    .order_by("-created_at")
                    .first()
                )

                if existing_assignment:
                    if existing_assignment.status == "ACTIVE":
                        return Response(
                            {
                                "status": "error",
                                "code": status.HTTP_409_CONFLICT,
                                "message": "Lead is already onboarded and active."
                            },
                            status=status.HTTP_409_CONFLICT
                        )

                    if existing_assignment.status == "MOVED_OUT":
                        return Response(
                            {
                                "status": "error",
                                "code": status.HTTP_409_CONFLICT,
                                "message": "Lead was onboarded earlier. Please use re-admit API."
                            },
                            status=status.HTTP_409_CONFLICT
                        )

                # ✅ STEP 2: Create NEW assignment (only once)
                room = data.get("room")

                assignment = LeadGroupHomeAssignment.objects.create(
                    lead=lead,
                    group_home=data["group_home"],
                    room=room,
                    check_in_date=data["check_in_date"],
                    status="ACTIVE",
                    assigned_by=request.user if request.user.is_authenticated else None
                )

                # ✅ Mark room occupied
                if room:
                    room.is_occupied = True
                    room.save(update_fields=["is_occupied"])



                # ✅ STEP 3: Update lead status
                lead.status = "COMPLETED"
                lead.save(update_fields=["status"])

                # ✅ STEP 4: Log onboarding BEFORE role promotion
                log_action(
                    request=request,
                    action="UPDATE",
                    entity_type="Lead/Onboarding",
                    entity_id=str(lead.uuid),
                    group_home=assignment.group_home,
                    message="Lead onboarding completed",
                    target_user=user,
                )

                # ✅ STEP 5: Promote user → RESIDENT (safe)
                resident_role = Role.objects.get(type="RESIDENT")
                if user.role.type != "RESIDENT":
                    user.role = resident_role
                    user.save(update_fields=["role"])


            return Response(
                {
                    "message": "Onboarding completed successfully.",
                    "assignment_uuid": str(assignment.uuid),
                    "resident_id": user.id
                },
                status=status.HTTP_200_OK
            )

        except Http404:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Lead not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except Exception as e:
            logger.error("Onboarding completion failed", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
    
#---------------MoveOutAPIView------------------------------------------------
class MoveOutAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PATCH": "onboarding.move_out",
    }

    @extend_schema(
    operation_id="move_out_lead",
    summary="Move out lead",
    description=(
        "Move a lead out from an active group home assignment, "
        "recording check-out date and reason."
    ),
    request=MoveOutSerializer,
    responses={200: OpenApiTypes.OBJECT},
    tags=["Lead Assignments"],
    )

    def patch(self, request, assignment_uuid):
        try:
            assignment = get_object_or_404(
                LeadGroupHomeAssignment,
                uuid=assignment_uuid,
                status="ACTIVE"
            )

            serializer = MoveOutSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            check_out_date = serializer.validated_data["check_out_date"]

            # Upper bound: cannot be in the future.
            if check_out_date > timezone.now().date():
                raise serializers.ValidationError(
                    {"check_out_date": "Check-out date cannot be in the future."}
                )

            # Lower bound: cannot predate the resident's check-in date.
            if assignment.check_in_date and check_out_date < assignment.check_in_date:
                raise serializers.ValidationError(
                    {
                        "check_out_date": (
                            "Check-out date cannot be before the check-in date "
                            f"({assignment.check_in_date.isoformat()})."
                        )
                    }
                )

            user = assignment.lead.user
            lead_role = Role.objects.get(type="LEAD")

            with transaction.atomic():
                room = assignment.room
                assignment.status = "MOVED_OUT"
                assignment.check_out_date = serializer.validated_data["check_out_date"]
                assignment.reason = serializer.validated_data.get("reason")
                assignment.resubmitted = False

                assignment.assigned_by = (
                    request.user if request.user.is_authenticated else None
                )

                assignment.save(
                    update_fields=[
                        "status",
                        "check_out_date",
                        "reason",
                        "resubmitted",
                        "assigned_by",
                        "updated_at"
                    ]
                )

                if room:
                    room.is_occupied = False
                    room.save(update_fields=["is_occupied"])

                # 2️⃣ Downgrade user role (Resident → Lead)
                if user.role.type == "RESIDENT":
                   user.role = lead_role
                   user.save(update_fields=["role"])

                   log_action(
                        request=request,
                        action="UPDATE",
                        entity_type="Resident/Moved-Out",
                        entity_id=str(assignment.uuid),
                        group_home=assignment.group_home,
                        message="Resident moved out",
                        target_user=user,
                    )


            return Response(
                {
                    "message": "Resident moved out successfully.",
                    "assignment_id": assignment.id,
                    "status": assignment.status,
                    "user_role": user.role.type,
                    "check_out_date": assignment.check_out_date,
                    "reason": assignment.reason
                },
                status=status.HTTP_200_OK
            )

        except (Http404, APIException):
            # Let DRF's exception handler return the proper status code
            # (400 for serializer validation errors such as a future
            # check_out_date, 404 for a missing/non-ACTIVE assignment)
            # instead of masking them as a generic 500.
            raise
        except Exception as e:
            logger.error("Move-out failed", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

#---------------ReAdmitAPIView-------------------------------------------------------
class ReAdmitAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PUT": "onboarding.readmit",
    }

    @extend_schema(
        operation_id="re_admit_lead",
        summary="Re-admit lead",
        description=(
            "Re-admit a previously moved-out or discharged lead "
            "by reactivating the group home assignment."
        ),
        request=ReAdmitSerializer,
        responses={200: OpenApiTypes.OBJECT},
        tags=["Lead Assignments"],
    )
    def put(self, request, assignment_uuid):
        try:
            assignment = get_object_or_404(
                LeadGroupHomeAssignment,
                uuid=assignment_uuid,
                status="MOVED_OUT"
            )

            serializer = ReAdmitSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data

            user = assignment.lead.user
            resident_role = Role.objects.get(type="RESIDENT")

            with transaction.atomic():
                new_room = data.get("room")
                new_group_home = data.get("group_home")

                assignment.room = new_room
                assignment.group_home = new_group_home
                assignment.status = data.get("status", "ACTIVE")
                assignment.check_in_date = data.get("check_in_date")
                assignment.financial_effective_date = data.get("financial_effective_date")
                assignment.check_out_date = None
                assignment.resubmitted = True

                assignment.assigned_by = (
                    request.user if request.user.is_authenticated else None
                )

                assignment.save()

                if new_room:
                    new_room.is_occupied = True
                    new_room.save(update_fields=["is_occupied"])

                # Promote Lead → Resident
                if user.role.type != "RESIDENT":
                    user.role = resident_role
                    user.save(update_fields=["role"])

                log_action(
                    request=request,
                    action="UPDATE",
                    entity_type="Resident/Re-Admit",
                    entity_id=str(assignment.uuid),
                    group_home=assignment.group_home,
                    message="Resident re-admitted",
                    target_user=user,
                )

            return Response(
                {
                    "message": "Resident re-admitted successfully.",
                    "assignment_uuid": assignment.uuid,
                    "status": assignment.status,
                    "check_in_date": assignment.check_in_date,
                    "financial_effective_date": assignment.financial_effective_date,
                    "group_home_uuid": str(assignment.group_home.uuid),
                    "group_home_name": assignment.group_home.name,
                    "room_uuid": str(assignment.room.uuid) if assignment.room else None,
                },
                status=status.HTTP_200_OK
            )

        except Exception as e:
            logger.error("Re-admit failed", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

#------------------ResidentListAPIView------------------------------------------------
class ResidentListAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        # DSP has adls.view (ASSIGNED_HOME) but NOT leads.view.
        # The view scope-filters via adls.view anyway, so use the same key
        # so that every role with adls.view can pass the gate.
        "GET": "adls.view",
    }
    # Agents & Guardians need the residents list to populate their portal
    allow_portal_roles = True

    @extend_schema(
        operation_id="list_residents",
        summary="List residents",
        description=(
            "Retrieve a paginated list of residents (leads assigned to group homes). "
            "If role/user_id is not provided, defaults to ADMIN access. "
            "Pass leads=true to include leads that are not yet assigned to a group home."
        ),
        tags=["Residents"],
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter("size", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("group_home", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("date", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("role", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("user_id", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter("user_uuid", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("leads", OpenApiTypes.BOOL, OpenApiParameter.QUERY),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        # ---------------- AUTH INPUT ----------------
        role_type = request.query_params.get("role")
        user_id = request.query_params.get("user_id")
        user_uuid = request.query_params.get("user_uuid")
        include_leads = request.query_params.get("leads", "").lower() in ("true", "1")

        acting_user = None

        # If BOTH role and a user identifier are provided → validate & apply filtering
        # Accepts either user_id (int PK) or user_uuid (UUID string)
        if role_type and (user_id or user_uuid):
            try:
                if user_uuid:
                    acting_user = User.objects.get(uuid=user_uuid)
                else:
                    user_id = int(user_id)
                    acting_user = User.objects.get(id=user_id)
            except (ValueError, User.DoesNotExist):
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Invalid user_id or user_uuid",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # SECURITY: a GUARDIAN/AGENT portal user is ALWAYS scoped to their own
        # linked residents, derived from the authenticated user — never trust
        # the client-supplied role/user_id/user_uuid. Without this override a
        # guardian who omits the role param falls through to unscoped ("ADMIN")
        # access and sees every resident (cross-tenant PHI leak). Staff/Admin
        # keep the existing role/user_uuid behaviour (e.g. admin viewing a
        # specific guardian's residents).
        auth_role_type = (
            getattr(getattr(request.user, "role", None), "type", "") or ""
        ).upper()
        if auth_role_type in ("GUARDIAN", "AGENT"):
            role_type = auth_role_type
            acting_user = request.user
        elif auth_role_type in ("RESIDENT", "LEAD"):
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_403_FORBIDDEN,
                    "message": "This role cannot list residents.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # ---------------- PAGINATION ----------------
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))

        # ---------------- SEARCH ----------------
        raw_search = request.query_params.get("search")
        search = (raw_search or "").strip()

        # ---------------- FILTERS ----------------
        status_filter = request.query_params.get("status")
        group_home_uuid = request.query_params.get("group_home")
        date_filter = request.query_params.get("date")

        # Parse date once if provided
        selected_date = None
        if date_filter:
            try:
                from datetime import datetime
                selected_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            except ValueError:
                pass

        # ================================================================
        # LEADS MODE: Query from Lead model (residents + unassigned leads)
        # ================================================================
        if include_leads:
            return self._get_with_leads(
                request, role_type, acting_user, page, size,
                search, status_filter, group_home_uuid, selected_date,
            )

        # ================================================================
        # DEFAULT MODE: Only residents (leads assigned to group homes)
        # ================================================================
        return self._get_residents_only(
            request, role_type, acting_user, page, size,
            search, status_filter, group_home_uuid, selected_date,
        )

    def _resolve_avatar_urls(self, users):
        """Resolve avatar URLs for a list of User objects."""
        avatar_urls_map = {}
        profile_picture_ids = [
            str(u.profile_picture_id)
            for u in users
            if getattr(u, "profile_picture_id", None)
        ]
        if profile_picture_ids:
            medias = Media.objects.filter(
                id__in=profile_picture_ids,
                status="active"
            )
            media_id_to_url = {str(m.id): m.get_file_url() for m in medias}
            avatar_urls_map = {
                u.id: media_id_to_url.get(str(u.profile_picture_id))
                for u in users
                if getattr(u, "profile_picture_id", None)
            }
        return avatar_urls_map

    def _get_residents_only(self, request, role_type, acting_user, page, size,
                            search, status_filter, group_home_uuid, selected_date):
        """Original behavior: only residents from LeadGroupHomeAssignment."""
        # Order by check_out_date (desc) for MOVED_OUT to show recently moved out first
        # For other statuses, order by created_at (desc)
        if status_filter == "MOVED_OUT":
            # Order by check_out_date descending, then updated_at as fallback
            # This ensures the most recently moved out residents appear first
            residents_qs = (
                LeadGroupHomeAssignment.objects
                .select_related("lead__user", "group_home")
                .order_by("-check_out_date", "-updated_at", "-created_at")
            )
        else:
            residents_qs = (
                LeadGroupHomeAssignment.objects
                .select_related("lead__user", "group_home")
                .order_by("-created_at")
            )

        # Role-based filtering
        if role_type and acting_user:
            if role_type == "GUARDIAN":
                residents_qs = residents_qs.filter(lead__guardian_id=acting_user.id, status="ACTIVE")
            elif role_type == "AGENT":
                residents_qs = residents_qs.filter(lead__agent_id=acting_user.id, status="ACTIVE")
            elif role_type == "ADMIN":
                pass
            else:
                return Response(
                    {"status": "error", "code": status.HTTP_403_FORBIDDEN, "message": "Unauthorized role"},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Scope filtering: use adls.view which has ASSIGNED_HOME for Nurse/Coordinator
        # and ALL for Admin/PD/PM/BCBA — correctly limits the residents shown.
        residents_qs = scope_queryset(
            request.user, residents_qs, "adls.view",
            group_home_path="group_home",
            assignment_status_path="status",
        )

        if status_filter:
            residents_qs = residents_qs.filter(status=status_filter)
        if group_home_uuid:
            residents_qs = residents_qs.filter(group_home__uuid=group_home_uuid)
        if selected_date:
            residents_qs = residents_qs.filter(created_at__date=selected_date)

        if search.lower() not in {"", "null", "undefined", "none"}:
            residents_qs = residents_qs.annotate(
                full_name=Concat(
                    "lead__user__first_name", Value(" "), "lead__user__last_name",
                    output_field=CharField(),
                )
            ).filter(
                Q(lead__user__first_name__icontains=search) |
                Q(lead__user__last_name__icontains=search) |
                Q(full_name__icontains=search)
            )

        paginator = Paginator(residents_qs, size)
        page_obj = paginator.get_page(page)

        users = [a.lead.user for a in page_obj.object_list if a.lead and a.lead.user]
        avatar_urls_map = self._resolve_avatar_urls(users)

        serializer = ResidentListSerializer(
            page_obj, many=True, context={"avatar_urls": avatar_urls_map},
        )

        return Response({
            "status": "success",
            "code": status.HTTP_200_OK,
            "message": "Residents fetched successfully",
            "data": {
                "results": serializer.data,
                "pagination": {
                    "page": page, "size": size,
                    "total_pages": paginator.num_pages,
                    "total_records": paginator.count,
                },
            },
        }, status=status.HTTP_200_OK)

    def _get_with_leads(self, request, role_type, acting_user, page, size,
                        search, status_filter, group_home_uuid, selected_date):
        """Leads mode: query Lead model directly (includes both residents and unassigned leads)."""
        leads_qs = (
            Lead.objects
            .select_related("user")
            .prefetch_related("group_home_assignments__group_home")
            .order_by("-created_at")
        )

        # Role-based filtering
        # NOTE: In leads mode, we show ALL leads assigned to the guardian/agent —
        # including those not yet placed in a group home (pure leads).
        # Do NOT filter by group_home_assignments__status here; that would hide
        # leads that haven't been admitted to a group home yet.
        if role_type and acting_user:
            if role_type == "GUARDIAN":
                leads_qs = leads_qs.filter(guardian_id=acting_user.id)
            elif role_type == "AGENT":
                leads_qs = leads_qs.filter(agent_id=acting_user.id)
            elif role_type == "ADMIN":
                pass
            else:
                return Response(
                    {"status": "error", "code": status.HTTP_403_FORBIDDEN, "message": "Unauthorized role"},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Scope filtering: use adls.view which has ASSIGNED_HOME for Nurse/Coordinator
        # and ALL for Admin/PD/PM/BCBA — correctly limits the residents shown.
        leads_qs = scope_queryset(
            request.user, leads_qs, "adls.view",
            group_home_path="group_home_assignments__group_home",
            assignment_status_path="group_home_assignments__status",
        )

        # Lead status filter
        if status_filter:
            leads_qs = leads_qs.filter(status=status_filter)

        # Group home filter (only leads that have an assignment in this group home)
        if group_home_uuid:
            leads_qs = leads_qs.filter(
                group_home_assignments__group_home__uuid=group_home_uuid
            )

        # Date filter
        if selected_date:
            leads_qs = leads_qs.filter(created_at__date=selected_date)

        # Search
        if search.lower() not in {"", "null", "undefined", "none"}:
            leads_qs = leads_qs.annotate(
                full_name=Concat(
                    "user__first_name", Value(" "), "user__last_name",
                    output_field=CharField(),
                )
            ).filter(
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(full_name__icontains=search)
            )

        paginator = Paginator(leads_qs, size)
        page_obj = paginator.get_page(page)

        users = [lead.user for lead in page_obj.object_list if lead.user]
        avatar_urls_map = self._resolve_avatar_urls(users)

        serializer = LeadDocumentSerializer(
            page_obj, many=True, context={"avatar_urls": avatar_urls_map},
        )

        return Response({
            "status": "success",
            "code": status.HTTP_200_OK,
            "message": "Residents and leads fetched successfully",
            "data": {
                "results": serializer.data,
                "pagination": {
                    "page": page, "size": size,
                    "total_pages": paginator.num_pages,
                    "total_records": paginator.count,
                },
            },
        }, status=status.HTTP_200_OK)



# ---------------- RejectReferralAPIView ----------------

class RejectReferralAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PATCH": "leads.reject",
    }

    @extend_schema(
        operation_id="reject_referral",
        summary="Reject referral",
        description=(
            "Reject a lead referral by marking its status as REJECTED "
            "and saving the rejection reason."
        ),
        request=RejectReferralSerializer,
        responses={200: OpenApiTypes.OBJECT},
        tags=["Leads"],
    )
    def patch(self, request, uuid):
        serializer = RejectReferralSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            lead = Lead.objects.get(uuid=uuid)

            # ❌ Prevent re-rejecting
            if lead.status == "REJECTED":
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Referral is already rejected",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            lead.status = "REJECTED"
            lead.reason = serializer.validated_data["reason"]
            lead.save(update_fields=["status", "reason", "updated_at"])

            log_action(
                request=request,
                action="REJECT",
                entity_type="Lead",
                entity_id=str(lead.uuid),
                message="Referral rejected",
            )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Referral rejected successfully",
                    "data": {
                        "uuid": str(lead.uuid),
                        "status": lead.status,
                        "reason": lead.reason,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except Lead.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Lead not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )


# ---------------- TransferResidentAPIView ----------------

# leads/views.py

class TransferResidentAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PUT": "onboarding.transfer_home",
    }

    @extend_schema(
        operation_id="transfer_resident",
        summary="Transfer resident to another group home/room",
        description="Update an ACTIVE lead_group_home_assignment with a new group home, room, and check-in date.",
        request=TransferResidentSerializer,
        responses={200: OpenApiTypes.OBJECT},
        tags=["Lead Assignments"],
    )
    

    def put(self, request, assignment_uuid):
        try:
            assignment = get_object_or_404(
                LeadGroupHomeAssignment,
                uuid=assignment_uuid,
                status="ACTIVE"
            )

            serializer = TransferResidentSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data

            old_room = assignment.room
            new_room = data.get("room")

            # ❌ Same room selected
            if old_room and new_room and old_room.id == new_room.id:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Resident is already assigned to this room."
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ❌ Room occupied
            if new_room and new_room.is_occupied:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Selected room is already occupied."
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            with transaction.atomic():

                # FREE old room
                if old_room:
                    old_room.is_occupied = False
                    old_room.save(update_fields=["is_occupied"])

                # Assign new room (same or different group home allowed)
                assignment.group_home = data["group_home"]
                assignment.room = new_room
                assignment.check_in_date = data["check_in_date"]
                assignment.save(
                    update_fields=[
                        "group_home",
                        "room",
                        "check_in_date",
                        "updated_at"
                    ]
                )

                # OCCUPY new room
                if new_room:
                    new_room.is_occupied = True
                    new_room.save(update_fields=["is_occupied"])

                log_action(
                    request=request,
                    action="UPDATE",
                    entity_type="Resident/Transfer",
                    entity_id=str(assignment.uuid),
                    group_home=assignment.group_home,
                    message="Resident transferred room/group home",
                    target_user=assignment.lead.user,
                )

            return Response(
                {
                    "status": "success",
                    "code": 200,
                    "message": "Resident transferred successfully",
                    "data": {
                        "assignment_uuid": str(assignment.uuid),
                        "lead_uuid": str(assignment.lead.uuid),
                        "group_home": assignment.group_home.name,
                        "room_id": assignment.room.id if assignment.room else None,
                        "check_in_date": assignment.check_in_date
                    }
                },
                status=status.HTTP_200_OK
            )

        except Exception as e:
            logger.error("Resident transfer failed", exc_info=True)
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Internal server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
