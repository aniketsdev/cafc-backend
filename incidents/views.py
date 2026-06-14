import logging
import traceback
from django.utils import timezone
from django.db import transaction
from django.core.paginator import Paginator
from django.db.models import F, Q, Value
from django.db.models.functions import Concat
from datetime import datetime

logger = logging.getLogger(__name__)
from leads.models import Lead
from .models import IncidentNotification
from accounts.models import User
from leads.models import LeadGroupHomeAssignment
from group_home.models import GroupHome, GroupHomeStaffAssignment

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from audit_logs.utils import log_action, get_active_group_home

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from django.contrib.contenttypes.models import ContentType
from media.models import Media

from accounts.models import Role, User
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import scope_queryset, scope_queryset_portal, check_scope_access, HasPermission, has_permission
from .models import (
    Incident,
    IncidentComment,
    IncidentMedicalFlag,
    IncidentLegalFlag,
    IncidentSocialFlag,
    IncidentVictimFlag,
    IncidentNotification,
    IncidentEdit,
)
from .serializers import IncidentSerializer, IncidentNotificationSerializer


# Valid notification type codes (must fit IncidentNotification.type max_length=50)
VALID_NOTIFICATION_TYPES = {choice[0] for choice in IncidentNotification.NOTIFICATION_TYPE_CHOICES}


# Fields tracked by the IncidentEdit audit log.
# Changes from IN_PROGRESS or PM_REVIEW_PENDING to these fields are recorded.
AUDIT_FIELDS = [
    "incident_description",
    "pre_incident_notes",
    "response_action",
    "location",
    "region",
    "agency_name",
    "incident_name",
]


# T24: fields immutable after the incident has been started.
# Restricted to the FK anchors (resident, group_home). Originally also locked
# incident_datetime and location, but those caused false-positive blocks on
# Sign & Complete when the form re-emitted the datetime in a slightly
# different format (TZ / microsecond drift) without the user actually
# editing it. Per spec section 4, PMs may edit content during review —
# the FK anchors stay locked because changing them would invalidate the
# whole record.
CORE_FIELDS = ["resident", "group_home"]


def _check_core_fields_unchanged(incident, request_data):
    """
    Returns dict of errors if any CORE_FIELDS in request_data differ from the
    incident's current values; empty dict if all unchanged or absent.

    Datetime fields are compared as parsed datetimes, not strings, so
    payload shapes like '2026-05-13T22:15:00' vs '2026-05-13T22:15:00Z'
    don't fire a false positive.
    """
    from django.utils.dateparse import parse_datetime

    errors = {}
    for f in CORE_FIELDS:
        if f not in request_data:
            continue
        incoming = request_data[f]
        if f in ("resident", "group_home"):
            # Could be incoming as PK, UUID (for resident slug field), or as nested dict
            current = getattr(incident, f"{f}_id", None)
            if isinstance(incoming, dict):
                incoming = incoming.get("id") or incoming.get("uuid")
            if str(incoming) != str(current):
                related_obj = getattr(incident, f, None)
                if related_obj is not None and str(incoming) == str(getattr(related_obj, "uuid", "")):
                    continue
                errors[f] = "Cannot be changed after the incident is started — use send-back to edit"
        elif f == "incident_datetime":
            current = getattr(incident, f, None)
            # Treat null/blank incoming as "no change submitted".
            if incoming in (None, ""):
                continue
            try:
                incoming_dt = parse_datetime(str(incoming)) if not hasattr(incoming, "tzinfo") else incoming
            except Exception:
                incoming_dt = None
            # Compare as UTC instants when both are datetimes.
            if incoming_dt and current:
                a = incoming_dt
                b = current
                if a.tzinfo is None and b.tzinfo is not None:
                    a = a.replace(tzinfo=b.tzinfo)
                elif b.tzinfo is None and a.tzinfo is not None:
                    b = b.replace(tzinfo=a.tzinfo)
                if a == b:
                    continue
            # Fall through to string compare if parsing failed.
            if str(incoming) != str(current) and incoming != current:
                errors[f] = "Cannot be changed after the incident is started — use send-back to edit"
        else:
            current = getattr(incident, f)
            # Treat null/blank incoming as "no change submitted".
            if incoming in (None, "") and current in (None, ""):
                continue
            if str(incoming) != str(current) and incoming != current:
                errors[f] = "Cannot be changed after the incident is started — use send-back to edit"
    return errors


def _staff_user_for_incident_role(incident, role_type):
    """
    Resolve the first active staff user for the incident's group home and role.
    Used for notification rows so the PDF can print staff names without
    requiring the frontend to pick a user.
    """
    if not incident or not incident.group_home_id:
        return None
    role_names_by_type = {
        GroupHomeStaffAssignment.RoleType.PROGRAM_MANAGER: "Program Manager",
        GroupHomeStaffAssignment.RoleType.PROGRAM_COORDINATOR: "Program Coordinator",
        GroupHomeStaffAssignment.RoleType.NURSE: "Nurse",
    }
    role_name = role_names_by_type.get(role_type)

    assignment = (
        GroupHomeStaffAssignment.objects.filter(
            group_home_id=incident.group_home_id,
            role_type=role_type,
            status=GroupHomeStaffAssignment.Status.ACTIVE,
            user__active=True,
            user__deleted_at__isnull=True,
        )
        .select_related("user")
        .order_by("id")
        .first()
    )
    if assignment:
        return assignment.user

    legacy_qs = User.objects.filter(
        Q(group_home_id=incident.group_home_id) |
        Q(group_homes__id=incident.group_home_id),
        active=True,
        deleted_at__isnull=True,
    )
    if role_name:
        legacy_qs = legacy_qs.filter(Q(role__type=role_type) | Q(role__name=role_name))
    else:
        legacy_qs = legacy_qs.filter(role__type=role_type)
    return legacy_qs.distinct().order_by("id").first()


def _notification_user_for_type(incident, notify_type, lead, request_user=None):
    if notify_type == "GUARDIAN":
        return lead.guardian if lead else None
    if notify_type == "SERVICE_COORDINATOR":
        return lead.agent if lead else None
    if notify_type == "PROGRAM_MANAGER":
        return incident.assigned_program_manager or _staff_user_for_incident_role(
            incident,
            GroupHomeStaffAssignment.RoleType.PROGRAM_MANAGER,
        )
    if notify_type == "ADDITIONAL_SERVICE_PROVIDER":
        return _staff_user_for_incident_role(
            incident,
            GroupHomeStaffAssignment.RoleType.PROGRAM_COORDINATOR,
        )
    if notify_type == "NURSING":
        return _staff_user_for_incident_role(
            incident,
            GroupHomeStaffAssignment.RoleType.NURSE,
        )
    if notify_type == "STAFF":
        return request_user
    return None


def _maybe_auto_start(incident, request):
    """
    Auto-transition a DRAFT incident directly to PM_REVIEW_PENDING when the
    HARD prerequisites are satisfied:
      - reporter_signature is set
      - resident is set

    The intermediate IN_PROGRESS state is skipped in the normal flow —
    DSP signing IS the act of submitting to the PM. Also creates the
    IncidentNotification row when a PM can be resolved.

    Silent on failure — the save still succeeds; status just stays DRAFT.
    """
    if not incident or incident.status != "DRAFT":
        if incident is not None:
            logger.info(
                f"[auto-start] ENTER skipped incident_id={getattr(incident, 'id', None)} "
                f"reason=not_draft current_status={getattr(incident, 'status', None)}"
            )
        return False
    inc_id = getattr(incident, "id", None)
    logger.info(
        f"[auto-start] ENTER incident_id={inc_id} status={incident.status} "
        f"group_home_id={incident.group_home_id} resident_id={incident.resident_id} "
        f"reporter_signature_id={incident.reporter_signature_id} "
        f"assigned_pm_id={incident.assigned_program_manager_id} "
        f"location={'set' if incident.location else 'EMPTY'} "
        f"incident_datetime={'set' if incident.incident_datetime else 'EMPTY'} "
        f"incident_description={'set' if incident.incident_description else 'EMPTY'} "
        f"actor_user_id={getattr(request.user, 'id', None)} "
        f"actor_email={getattr(request.user, 'email', None)} "
        f"actor_role={getattr(getattr(request.user, 'role', None), 'name', None)}"
    )
    try:
        if not incident.reporter_signature_id:
            logger.warning(
                f"[auto-start] SKIP incident_id={inc_id} reason=no_reporter_signature"
            )
            return False
        if not incident.resident_id:
            logger.warning(
                f"[auto-start] SKIP incident_id={inc_id} reason=no_resident"
            )
            return False
        # If the PM wasn't already snapshotted on the incident, derive it
        # from the resident's onboarding context so we can hand off review.
        if not incident.assigned_program_manager_id:
            try:
                from incidents.context import resident_incident_context
                ctx = resident_incident_context(incident.resident)
                derived_pm = ctx.get("assigned_program_manager")
                if derived_pm:
                    incident.assigned_program_manager = derived_pm
            except Exception:
                pass
        now = timezone.now()
        incident.status = "PM_REVIEW_PENDING"
        incident.started_at = now
        incident.submitted_for_review_at = now
        if not incident.program_manager_email and incident.assigned_program_manager:
            incident.program_manager_email = incident.assigned_program_manager.email
        if incident.program_manager_email and not incident.coordinator_email:
            incident.coordinator_email = incident.program_manager_email
        incident.save(update_fields=[
            "status",
            "started_at",
            "submitted_for_review_at",
            "assigned_program_manager",
            "program_manager_email",
            "coordinator_email",
            "updated_at",
        ])

        # Notify the assigned PM (idempotent — skip if already created).
        # by_whom records the actor (typically the DSP creating/saving the
        # incident) and method_of_contact reflects the email notification
        # that is dispatched to the PM on hand-off.
        actor_name = (
            f"{request.user.first_name or ''} {request.user.last_name or ''}".strip()
            or getattr(request.user, "email", "")
        )
        try:
            from incidents.models import IncidentNotification
            if incident.assigned_program_manager_id:
                already = IncidentNotification.objects.filter(
                    incident=incident,
                    type="PROGRAM_MANAGER",
                    user=incident.assigned_program_manager,
                ).exists()
                if not already:
                    IncidentNotification.objects.create(
                        incident=incident,
                        type="PROGRAM_MANAGER",
                        user=incident.assigned_program_manager,
                        notify=True,
                        notify_date=now.date(),
                        notify_time=now.time(),
                        method_of_contact="EMAIL",
                        by_whom=actor_name,
                    )
        except Exception as e:
            logger.warning(
                f"[auto-start] PM notification create failed "
                f"incident_id={incident.id} err={type(e).__name__}: {e}"
            )

        try:
            log_action(
                request=request,
                action="STATUS_CHANGE",
                entity_type="Incident",
                entity_id=str(incident.uuid),
                group_home=getattr(incident, "group_home", None),
                message=f"DRAFT → PM_REVIEW_PENDING (auto-start on save) by {getattr(request.user, 'email', '?')}",
            )
        except Exception:
            pass
        logger.info(
            f"[auto-start] OK incident_id={inc_id} new_status=PM_REVIEW_PENDING "
            f"assigned_pm_id={incident.assigned_program_manager_id}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"[auto-start] failed for incident_id={getattr(incident, 'id', None)} "
            f"err={type(e).__name__}: {e}"
        )
        return False


def _log_request_snapshot(tag, request, extra=""):
    """
    Emit a compact, log-friendly snapshot of an incoming request so we can
    trace what the client actually sent. Safe to call at the top of any view.
    """
    try:
        import json as _json
        user_id = getattr(request.user, "id", None)
        user_email = getattr(request.user, "email", None)
        # request.data is a QueryDict/dict; stringify defensively.
        try:
            body_str = _json.dumps(request.data, default=str)[:4000]
        except Exception:
            body_str = str(request.data)[:4000]
        logger.info(
            f"[sig-trace] {tag} incoming "
            f"user_id={user_id} user_email={user_email} "
            f"path={request.path} method={request.method} "
            f"content_type={request.content_type} {extra} "
            f"body={body_str}"
        )
    except Exception as e:
        logger.warning(f"[sig-trace] request snapshot failed: {e}")


def _log_response_snapshot(tag, incident, extra=""):
    """Emit a compact snapshot of an incident after create/update so we can see
    exactly what the serializer will return to the client."""
    try:
        if incident is None:
            logger.info(f"[sig-trace] {tag} response incident=None {extra}")
            return
        from incidents.serializers import IncidentSerializer as _IS
        data = _IS(incident).data
        signature = data.get("signature")
        signature_url = data.get("signature_url")
        logger.info(
            f"[sig-trace] {tag} response "
            f"incident_id={getattr(incident, 'id', None)} "
            f"incident_uuid={getattr(incident, 'uuid', '')} "
            f"signature_url_present={bool(signature_url)} "
            f"signature_present={bool(signature)} "
            f"signature_url={signature_url} "
            f"signature_obj={signature} {extra}"
        )
    except Exception as e:
        logger.warning(f"[sig-trace] response snapshot failed: {e}")


def _normalize_notification_type(item):
    """
    Accept either a string (type code) or a dict with 'notify_to' or 'type'.
    Returns a valid type code or None if invalid (avoids 'value too long' when frontend sends objects).
    """
    if isinstance(item, str) and item in VALID_NOTIFICATION_TYPES:
        return item
    if isinstance(item, dict):
        code = item.get("notify_to") or item.get("type")
        if isinstance(code, str) and code in VALID_NOTIFICATION_TYPES:
            return code
    return None


def _get_user_profile_signature_media(user):
    """Return the user's profile signature Media row, or None."""
    if not user:
        logger.warning("[sig-duplicate] lookup: user is None")
        return None
    user_id = getattr(user, "id", None)
    media_id = getattr(user, "signature_media_id", None)
    if media_id:
        m = Media.objects.filter(id=str(media_id).strip(), status="active").first()
        if m:
            logger.info(
                f"[sig-duplicate] lookup OK via user.signature_media_id "
                f"user_id={user_id} media_id={m.id} s3_key={m.s3_key}"
            )
            return m
        logger.warning(
            f"[sig-duplicate] user.signature_media_id={media_id} set but no active Media row "
            f"user_id={user_id}"
        )
    try:
        ct = ContentType.objects.get_for_model(user.__class__)
        m = Media.objects.filter(
            content_type=ct,
            object_id=user.id,
            status="active",
            alt_text__icontains="signature",
        ).order_by("-uploaded_at").first()
        if m:
            logger.info(
                f"[sig-duplicate] lookup OK via generic relation "
                f"user_id={user_id} media_id={m.id} s3_key={m.s3_key} alt={m.alt_text!r}"
            )
        else:
            logger.warning(
                f"[sig-duplicate] NO profile signature found for user_id={user_id}"
            )
        return m
    except Exception as e:
        logger.error(
            f"[sig-duplicate] lookup error user_id={user_id} err={e}\n{traceback.format_exc()}"
        )
        return None


def _duplicate_media_to_incident(source_media, incident, user):
    """
    Server-side copy of an existing Media's S3 object into a new
    incident-scoped Media row. Avoids the browser-side fetch of presigned URLs
    (which fails with a CORS-masked 403 once the URL expires).

    Returns the new Media instance, or None if the copy could not be performed.
    """
    incident_id = getattr(incident, "id", None)
    incident_uuid = str(getattr(incident, "uuid", "")) if incident is not None else ""
    user_id = getattr(user, "id", None)

    if not source_media or not getattr(source_media, "s3_key", None):
        logger.warning(
            f"[sig-duplicate] copy aborted: no source/s3_key "
            f"has_source={bool(source_media)} incident_id={incident_id} user_id={user_id}"
        )
        return None

    import uuid as uuid_lib
    from django.conf import settings
    from media.s3_client import get_s3_client

    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
    if not bucket:
        logger.error(
            f"[sig-duplicate] copy aborted: AWS_STORAGE_BUCKET_NAME empty "
            f"incident_id={incident_id} user_id={user_id}"
        )
        return None

    src_key = source_media.s3_key
    src_bucket = source_media.s3_bucket or bucket
    src_ext = (source_media.file_extension or "").strip().lstrip(".")
    if not src_ext and "." in src_key:
        src_ext = src_key.rsplit(".", 1)[-1]
    src_ext = src_ext or "png"

    new_filename = f"signature-{uuid_lib.uuid4().hex}.{src_ext}"
    now = timezone.now()
    dst_key = f"{now.year}/{now.month:02d}/{now.day:02d}/{new_filename}"

    logger.info(
        f"[sig-duplicate] copying S3 object "
        f"src=s3://{src_bucket}/{src_key} dst=s3://{bucket}/{dst_key} "
        f"incident_id={incident_id} incident_uuid={incident_uuid} "
        f"user_id={user_id} source_media_id={source_media.id}"
    )

    try:
        s3 = get_s3_client()
        s3.copy_object(
            Bucket=bucket,
            Key=dst_key,
            CopySource={"Bucket": src_bucket, "Key": src_key},
        )
    except Exception as e:
        logger.error(
            f"[sig-duplicate] s3.copy_object FAILED "
            f"src=s3://{src_bucket}/{src_key} dst=s3://{bucket}/{dst_key} "
            f"incident_id={incident_id} user_id={user_id} "
            f"err={type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise

    content_type_incident = ContentType.objects.get(app_label="incidents", model="incident")
    media = Media(
        original_filename=source_media.original_filename or new_filename,
        file_type=source_media.file_type,
        mime_type=source_media.mime_type or "image/png",
        file_extension=src_ext,
        file_size=source_media.file_size,
        content_type=content_type_incident,
        object_id=incident.id,
        alt_text=source_media.alt_text or f"Incident {incident.uuid} signature",
        description=source_media.description,
        uploaded_by=user,
        status="active",
        s3_key=dst_key,
        s3_bucket=bucket,
        width=source_media.width,
        height=source_media.height,
    )
    # Assign FileField name directly so save() doesn't try to upload bytes again.
    media.file.name = dst_key
    try:
        media.save()
    except Exception as e:
        logger.error(
            f"[sig-duplicate] Media.save() FAILED dst_key={dst_key} "
            f"incident_id={incident_id} user_id={user_id} "
            f"err={type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        raise

    logger.info(
        f"[sig-duplicate] OK created incident-scoped signature "
        f"new_media_id={media.id} dst_key={dst_key} "
        f"incident_id={incident_id} incident_uuid={incident_uuid} user_id={user_id}"
    )
    return media


# =========================
# LIST & CREATE INCIDENTS
# =========================
class IncidentListCreateAPIView(APIView):
    """
    Handles GET (list) and POST (create) operations for incidents.
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "incidents.view",
        "POST": "incidents.create",
    }
    # Agents & Guardians can view incidents for their residents
    allow_portal_roles = True

    # -------------------------
    # GET: List incidents with optional filters, search, and pagination
    # -------------------------
    @extend_schema(
    operation_id="list_incidents",
    summary="List incidents",
    description="Fetch incidents with pagination, status filter, resident search, resident UUID and date filter",
    tags=["Incident"],
    parameters=[
        OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, default=1),
        OpenApiParameter("size", OpenApiTypes.INT, OpenApiParameter.QUERY, default=10),

        OpenApiParameter(
            "status",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            description="Filter by status (OPEN / CLOSED / ACKNOWLEDGED)",
        ),
        OpenApiParameter(
            name="group_home_uuid",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description="Filter incidents by group home uuid",
            required=False,
        ),

        OpenApiParameter(
            "search",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            description="Search resident by full name",
        ),

        # ✅ NEW
        OpenApiParameter(
            name="resident_uuid",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.QUERY,
            description="Filter incidents by resident UUID",
            required=False,
        ),
        OpenApiParameter(
            name="date",
            type=OpenApiTypes.DATE,
            location=OpenApiParameter.QUERY,
            description="Filter by incident date (YYYY-MM-DD)",
            required=False,
        ),
        # 🔹 Distinct Latest
        OpenApiParameter(
            name="distinctLatest",
            type=OpenApiTypes.BOOL,
            location=OpenApiParameter.QUERY,
            description="If true, returns latest incident per resident",
            required=False,
        ),

        # 🔹 Role
        OpenApiParameter(
            name="role",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description="Role context for filtering (ADMIN, GUARDIAN, AGENT)",
            required=False,
        ),

        # 🔹 Role user UUID (Guardian or Agent)
        OpenApiParameter(
            name="uuid",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description="Guardian or Agent user UUID (required when role is GUARDIAN or AGENT)",
            required=False,
        ),
     ],
    )

   

    # def get(self, request):
        

    #     # -------------------------
    #     # QUERY PARAMS
    #     # -------------------------
    #     page = int(request.query_params.get("page", 1))
    #     size = int(request.query_params.get("size", 10))
    #     status_param = request.query_params.get("status")
    #     search = request.query_params.get("search", "").strip()
    #     distinct_latest = request.query_params.get("distinctLatest")
    #     distinct_latest = (
    #         distinct_latest is not None
    #         and distinct_latest.lower() == "true"
    #     )
        

        
    #     # ✅ NEW
    #     resident_uuid = request.query_params.get("resident_uuid")
    #     group_home_id = request.query_params.get("group_home_id")
        
        

    #     date_param = request.query_params.get("date")

    #     queryset = (
    #         Incident.objects.filter(deleted_at__isnull=True)
    #         .select_related("resident")      

    #     )

    #     # -------------------------
    #     # STATUS FILTER
    #     # -------------------------
    #     if status_param is not None:
    #         if status_param.lower() == "true":
    #             queryset = queryset.filter(status=True)
    #         elif status_param.lower() == "false":
    #             queryset = queryset.filter(status=False)

    #     # -------------------------
    #     # RESIDENT UUID FILTER ✅
    #     # -------------------------
       
    #     if resident_uuid:
    #         resident = User.objects.filter(uuid=resident_uuid).first()
    #         if not resident:
    #             return Response(
    #                 {"status": "error", "message": "Resident not found"},
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )

    #         queryset = queryset.filter(resident=resident)


    #         # -------------------------
    #         # GROUP HOME FILTER  ✅ OUTSIDE
    #         # -------------------------
    #     if group_home_id:

    #             try:
    #                 group_home_id = int(group_home_id)
    #             except ValueError:
    #                 return Response(
    #                     {"status": "error", "message": "group_home_id must be integer"},
    #                     status=status.HTTP_400_BAD_REQUEST,
    #                 )

    #             assignments = LeadGroupHomeAssignment.objects.filter(
    #                 group_home_id=group_home_id,
    #                 status="ACTIVE"
    #             )

    #             print("🔥 GROUP HOME FILTER EXECUTING 🔥", group_home_id)
    #             print("ASSIGNMENTS:", assignments.count())

    #             resident_ids = list(assignments.values_list("lead__user_id", flat=True))
    #             print("RESIDENT IDS:", resident_ids)

    #             queryset = queryset.filter(resident_id__in=resident_ids)


    #     # DATE FILTER ✅
    #     # -------------------------
    #     if date_param:
    #         try:
    #             filter_date = datetime.strptime(date_param, "%Y-%m-%d").date()
    #         except ValueError:
    #             return Response(
    #                 {"status": "error", "message": "Invalid date format. Use YYYY-MM-DD"},
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )

    #         queryset = queryset.filter(incident_datetime__date=filter_date)

    #     # -------------------------
    #     # SEARCH BY RESIDENT NAME
    #     # -------------------------
    #     if search:
    #         resident_role = Role.objects.filter(type__iexact="resident").first()
    #         if not resident_role:
    #             return Response(
    #                 {"status": "error", "message": "Resident role not configured"},
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )

    #         users_qs = User.objects.annotate(
    #             full_name=Concat(F("first_name"), Value(" "), F("last_name"))
    #         ).filter(full_name__icontains=search)

    #         if not users_qs.exists():
    #             return Response(
    #                 {"status": "error", "message": "User not found"},
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )

    #         resident_users = users_qs.filter(role=resident_role)
    #         if not resident_users.exists():
    #             return Response(
    #                 {"status": "error", "message": "Incident Not Found"},
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )

    #         queryset = queryset.filter(
    #             resident_id__in=resident_users.values_list("id", flat=True)
    #         )

    #     queryset = queryset.order_by("-created_at")

    #     # -------------------------
    #     # PAGINATION
    #     # -------------------------
    #     paginator = Paginator(queryset, size)
    #     paginated_data = paginator.get_page(page)

    #     serializer = IncidentSerializer(paginated_data, many=True)

    #     return Response(
    #         {
    #             "status": "success",
    #             "code": 200,
    #             "message": "Incidents fetched successfully",
    #             "data": {
    #                 "results": serializer.data,
    #                 "pagination": {
    #                     "page": page,
    #                     "size": size,
    #                     "total_pages": paginator.num_pages,
    #                     "total_records": paginator.count,
    #                 },
    #             },
    #         },
    #         status=status.HTTP_200_OK,
    #     )

    def get(self, request):

        # -------------------------
        # QUERY PARAMS
        # -------------------------
        page = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("size", 10))

        status_param = request.query_params.get("status")
        search = request.query_params.get("search", "").strip()

        resident_uuid = request.query_params.get("resident_uuid")
        group_home_uuid = request.query_params.get("group_home_uuid")
        date_param = request.query_params.get("date")

        role_param = request.query_params.get("role")
        role_uuid = request.query_params.get("uuid")

        assigned_to_me = (request.query_params.get("assigned_to_me") or "").lower() == "true"

        distinct_latest = request.query_params.get("distinctLatest")
        distinct_latest = (
            distinct_latest is not None
            and distinct_latest.lower() == "true"
        )

        # -------------------------
        # BASE QUERYSET
        # -------------------------
        queryset = (
            Incident.objects
            .filter(deleted_at__isnull=True)
            .select_related("resident")
        )

        # Scope to assigned group home for ASSIGNED_HOME users
        if request.user.is_authenticated:
            queryset = scope_queryset(
                request.user, queryset, "incidents.view",
                group_home_path="resident__leads__group_home_assignments__group_home",
            )

            # SECURITY: GUARDIAN/AGENT portal users are scoped to their OWN
            # linked residents, enforced from request.user — independent of the
            # optional ?role=&uuid= query params below. Without this a portal
            # user who omits those params sees every resident's incidents.
            queryset = scope_queryset_portal(
                request.user, queryset,
                guardian_path="resident__leads__guardian_id",
                agent_path="resident__leads__agent_id",
                active_assignment_path="resident__leads__group_home_assignments__status",
            )

        # PM and PC roles are always scoped to their assigned group homes regardless
        # of their permission scope setting, so they only see incidents from homes
        # they are actually assigned to via GroupHomeStaffAssignment.
        if request.user.is_authenticated:
            role_name = getattr(getattr(request.user, "role", None), "name", "")
            if role_name in ("Program Manager", "Program Coordinator"):
                staff_ids = set(
                    GroupHomeStaffAssignment.objects
                    .filter(user=request.user, status="ACTIVE")
                    .values_list("group_home_id", flat=True)
                )
                m2m_ids = set(
                    request.user.group_homes.values_list("id", flat=True)
                ) if hasattr(request.user, "group_homes") else set()
                user_home_ids = list(staff_ids | m2m_ids)
                if not user_home_ids and getattr(request.user, "group_home_id", None):
                    user_home_ids = [request.user.group_home_id]
                if user_home_ids:
                    queryset = queryset.filter(group_home_id__in=user_home_ids)
                else:
                    queryset = queryset.none()

        # T25: PM Review Queue filter — only incidents assigned to the current user
        if assigned_to_me and request.user.is_authenticated:
            queryset = queryset.filter(assigned_program_manager=request.user)

        # -------------------------
        # ROLE-BASED FILTERING
        # (Only when role is passed)
        # -------------------------
        if role_param:
            role_param = role_param.upper()

            if role_param in ["GUARDIAN", "AGENT"]:
                if not role_uuid or not str(role_uuid).strip():
                    return Response(
                        {
                            "status": "error",
                            "message": f"uuid is required when role is {role_param}"
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                role_uuid_str = str(role_uuid).strip()

                if role_param == "GUARDIAN":
                    queryset = queryset.filter(
                        resident__leads__guardian__uuid=role_uuid_str
                    )

                elif role_param == "AGENT":
                    queryset = queryset.filter(
                        resident__leads__agent__uuid=role_uuid_str
                    )
                
                # Exclude moved-out residents for Guardian and Agent portals
                queryset = queryset.filter(
                    resident__leads__group_home_assignments__status="ACTIVE"
                )

        # -------------------------
        # STATUS FILTER
        # -------------------------
        # if status_param is not None:
        #     if status_param.lower() == "true":
        #         queryset = queryset.filter(status=True)
        #     elif status_param.lower() == "false":
        #         queryset = queryset.filter(status=False)

        if status_param:
            status_param = status_param.upper()

            valid_statuses = [choice[0] for choice in Incident.Status.choices]

            if status_param not in valid_statuses:
                return Response(
                    {
                        "status": "error",
                        "message": "Invalid status value",
                        "allowed_values": valid_statuses,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            queryset = queryset.filter(status=status_param)

        # -------------------------
        # RESIDENT UUID FILTER
        # -------------------------
        if resident_uuid:
            resident = User.objects.filter(uuid=resident_uuid).first()
            if not resident:
                return Response(
                    {"status": "error", "message": "Resident not found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(resident=resident)

        # -------------------------
        # GROUP HOME FILTER
        # -------------------------
        if group_home_uuid:
            assignments = LeadGroupHomeAssignment.objects.filter(
                group_home__uuid=group_home_uuid,
                status="ACTIVE"
            )

            resident_ids = assignments.values_list(
                "lead__user_id",
                flat=True
            )

            queryset = queryset.filter(resident_id__in=resident_ids)

        # -------------------------
        # DATE FILTER
        # -------------------------
        if date_param:
            try:
                filter_date = datetime.strptime(
                    date_param, "%Y-%m-%d"
                ).date()
            except ValueError:
                return Response(
                    {
                        "status": "error",
                        "message": "Invalid date format. Use YYYY-MM-DD"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            queryset = queryset.filter(
                incident_datetime__date=filter_date
            )

        # -------------------------
        # SEARCH BY RESIDENT NAME
        # -------------------------
        if search:
            resident_role = Role.objects.filter(type__iexact="resident").first()
            if resident_role:
                resident_ids = (
                    User.objects.annotate(
                        full_name=Concat(F("first_name"), Value(" "), F("last_name"))
                    )
                    .filter(full_name__icontains=search, role=resident_role)
                    .values_list("id", flat=True)
                )
                queryset = queryset.filter(resident_id__in=resident_ids)
            else:
                queryset = queryset.none()

        queryset = queryset.order_by("-created_at")

        # -------------------------
        # DISTINCT LATEST PER RESIDENT
        # (ONLY when explicitly requested)
        # -------------------------
        if distinct_latest:
            queryset = (
                queryset
                .order_by("resident_id", "-created_at")
                .distinct("resident_id")
            )
        else:
            queryset = queryset.order_by("-created_at")

        # -------------------------
        # PAGINATION
        # -------------------------
        paginator = Paginator(queryset, size)
        paginated_data = paginator.get_page(page)

        serializer = IncidentSerializer(paginated_data, many=True)

        return Response(
            {
                "status": "success",
                "code": 200,
                "message": "Incidents fetched successfully",
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


    # -------------------------
    # POST: Create new incident with flags
    # -------------------------
    @extend_schema(
        operation_id="create_incident",
        summary="Create incident",
        tags=["Incident"],
        request=IncidentSerializer,
    )
    def post(self, request):
        _log_request_snapshot("CREATE", request)
        # status is server-managed: every POST creates a DRAFT regardless of
        # what the client sends. Strip any caller-supplied status before
        # validation so we don't reject legacy values like "OPEN".
        if hasattr(request.data, "_mutable"):
            request.data._mutable = True
        request.data.pop("status", None)
        serializer = IncidentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info(
            f"[sig-trace] CREATE serializer.validated_data keys="
            f"{list(serializer.validated_data.keys())}"
        )

        # commit: extract flags from validated data
        medical_flags = serializer.validated_data.pop("medical_flags", [])
        legal_flags = serializer.validated_data.pop("legal_flags", [])
        social_flags = serializer.validated_data.pop("social_flags", [])
        victim_flags = serializer.validated_data.pop("victim_flags", [])
        notifications = request.data.get("notifications", [])
        signature_in_payload = "signature_media_id" in request.data
        # IMPORTANT: not a model field; must be removed before Incident.objects.create(**validated_data)
        signature_media_id = serializer.validated_data.pop("signature_media_id", None) if signature_in_payload else None
        # Pop the duplicate flag so it doesn't reach Incident.objects.create.
        duplicate_signature_from_profile = serializer.validated_data.pop(
            "duplicate_signature_from_profile", False
        )

        # Resolve new write-only FK inputs (group_home_uuid, assigned_program_manager_uuid).
        gh_uuid = serializer.validated_data.pop("group_home_uuid", None)
        if gh_uuid:
            from group_home.models import GroupHome
            try:
                serializer.validated_data["group_home"] = GroupHome.objects.get(uuid=gh_uuid)
            except GroupHome.DoesNotExist:
                return Response(
                    {"status": "error", "errors": {"group_home_uuid": "Group home not found"}},
                    status=400,
                )

        pm_uuid = serializer.validated_data.pop("assigned_program_manager_uuid", None)
        if pm_uuid:
            from accounts.models import User as _User
            try:
                serializer.validated_data["assigned_program_manager"] = _User.objects.get(uuid=pm_uuid)
            except _User.DoesNotExist:
                return Response(
                    {"status": "error", "errors": {"assigned_program_manager_uuid": "User not found"}},
                    status=400,
                )

        # pm_signature_media_id is for the pm-signoff endpoint (Task 21), but pop it here
        # so it doesn't reach Incident.objects.create / setattr loop.
        serializer.validated_data.pop("pm_signature_media_id", None)

        # Resident-derived context (revised 2026-05-19): if the caller didn't
        # supply group_home / assigned_program_manager, derive them from the
        # resident's onboarding state. Also snapshot the PM email if blank.
        resident_obj = serializer.validated_data.get("resident")
        if resident_obj is not None:
            from incidents.context import resident_incident_context
            ctx = resident_incident_context(resident_obj)
            if not serializer.validated_data.get("group_home") and ctx["group_home"]:
                serializer.validated_data["group_home"] = ctx["group_home"]
            if not serializer.validated_data.get("assigned_program_manager") and ctx["assigned_program_manager"]:
                serializer.validated_data["assigned_program_manager"] = ctx["assigned_program_manager"]
            derived_pm = serializer.validated_data.get("assigned_program_manager")
            if derived_pm and not serializer.validated_data.get("program_manager_email"):
                serializer.validated_data["program_manager_email"] = derived_pm.email
            if (
                serializer.validated_data.get("program_manager_email")
                and not serializer.validated_data.get("coordinator_email")
            ):
                serializer.validated_data["coordinator_email"] = (
                    serializer.validated_data["program_manager_email"]
                )


        logger.info(
            f"[sig-duplicate] CREATE payload user_id={getattr(request.user, 'id', None)} "
            f"signature_in_payload={signature_in_payload} "
            f"signature_media_id={signature_media_id} "
            f"duplicate_signature_from_profile={duplicate_signature_from_profile}"
        )

        with transaction.atomic():
            # commit: create main incident object
            serializer.validated_data["reported_by"] = request.user
            incident = Incident.objects.create(**serializer.validated_data)

            # ---------------- SIGNATURE (incident-scoped) ---------------- #
            try:
                content_type_incident = ContentType.objects.get(app_label="incidents", model="incident")

                # Server-side duplicate of the user's profile signature.
                # Used when the frontend wants to reuse the profile signature without
                # re-fetching its presigned S3 URL (which fails with CORS-masked 403
                # once the ~1h URL expires).
                if duplicate_signature_from_profile and not (
                    signature_in_payload and signature_media_id
                ):
                    logger.info(
                        f"[sig-duplicate] CREATE branch=duplicate_from_profile "
                        f"incident_id={incident.id} incident_uuid={incident.uuid}"
                    )
                    source = _get_user_profile_signature_media(request.user)
                    if source:
                        new_media = _duplicate_media_to_incident(source, incident, request.user)
                        # Also bind the new explicit reporter_signature FK so
                        # /start's validator sees it.
                        if new_media is not None:
                            incident.reporter_signature = new_media
                            incident.save(update_fields=["reporter_signature", "updated_at"])
                    else:
                        logger.warning(
                            f"[sig-duplicate] CREATE flag set but no profile signature "
                            f"user_id={getattr(request.user, 'id', None)}"
                        )
                elif signature_in_payload:
                    if signature_media_id is None or (isinstance(signature_media_id, str) and not signature_media_id.strip()):
                        # Clear signature: soft-delete existing signature media linked to this incident
                        Media.objects.filter(
                            content_type=content_type_incident,
                            object_id=incident.id,
                            status="active",
                            alt_text__icontains="signature",
                        ).update(status="deleted")
                        # Also clear the explicit FK so /start correctly reports missing signature.
                        incident.reporter_signature = None
                        incident.save(update_fields=["reporter_signature", "updated_at"])
                    else:
                        media_id = str(signature_media_id).strip()
                        media_obj = Media.objects.filter(id=media_id, status="active").first()
                        if media_obj:
                            media_obj.content_type = content_type_incident
                            media_obj.object_id = incident.id
                            if not (media_obj.alt_text and "signature" in media_obj.alt_text.lower()):
                                media_obj.alt_text = f"Incident {incident.uuid} signature"
                            media_obj.save()
                            # Bind the new explicit reporter_signature FK so
                            # /start's validator sees it.
                            incident.reporter_signature = media_obj
                            incident.save(update_fields=["reporter_signature", "updated_at"])
                        # Deactivate any other signature media linked to this incident (keep single active signature)
                        Media.objects.filter(
                            content_type=content_type_incident,
                            object_id=incident.id,
                            status="active",
                            alt_text__icontains="signature",
                        ).exclude(id=media_id).update(status="deleted")
            except Exception as e:
                # never fail incident create due to signature linking
                logger.error(
                    f"[sig-duplicate] CREATE signature handling FAILED — incident kept, signature missing "
                    f"incident_id={getattr(incident, 'id', None)} "
                    f"incident_uuid={getattr(incident, 'uuid', '')} "
                    f"user_id={getattr(request.user, 'id', None)} "
                    f"err={type(e).__name__}: {e}\n{traceback.format_exc()}"
                )

            log_action(
                request=request,
                action="CREATE",
                entity_type="Incident",
                entity_id=str(incident.uuid),
                group_home=getattr(incident, "group_home", None),
                message="Incident created",
            )


            # commit: create associated flags
            for flag in medical_flags:
                IncidentMedicalFlag.objects.create(incident=incident, **flag)
            for flag in legal_flags:
                IncidentLegalFlag.objects.create(incident=incident, **flag)
            for flag in social_flags:
                IncidentSocialFlag.objects.create(incident=incident, **flag)
            for flag in victim_flags:
                IncidentVictimFlag.objects.create(incident=incident, **flag)
            resident = incident.resident

            lead = Lead.objects.filter(user=resident).first()

            guardian_user = lead.guardian if lead else None
            agent_user = lead.agent if lead else None

            for raw in notifications:
                notify_type = _normalize_notification_type(raw)
                if notify_type is None:
                    continue

                user = _notification_user_for_type(
                    incident,
                    notify_type,
                    lead,
                    request.user,
                )

                # Extract notify_date / notify_time / method_of_contact / by_whom from payload (if dict)
                raw_notify_date = None
                raw_notify_time = None
                raw_method_of_contact = None
                raw_by_whom = None
                if isinstance(raw, dict):
                    raw_notify_date = raw.get("notify_date") or None
                    raw_notify_time = raw.get("notify_time") or None
                    raw_method_of_contact = raw.get("method_of_contact") or None
                    raw_by_whom = raw.get("by_whom") or None

                IncidentNotification.objects.create(
                    incident=incident,
                    type=notify_type,
                    user=user,
                    notify=True,
                    notify_date=raw_notify_date,
                    notify_time=raw_notify_time,
                    method_of_contact=raw_method_of_contact,
                    by_whom=raw_by_whom,
                )

            # ---------------- COMMENT (inline) ---------------- #
            comment_text = request.data.get("comment")
            if comment_text:
                user_role = getattr(request.user, "role", None)
                IncidentComment.objects.create(
                    incident=incident,
                    comment=comment_text,
                    created_by=request.user,
                    role=user_role,
                )

        # Auto-start: when a DSP creates an incident WITH a signature already
        # attached, jump straight to PM_REVIEW_PENDING instead of leaving it
        # in DRAFT. If signature/PM aren't resolvable yet, status stays DRAFT
        # and the next save will retry.
        _maybe_auto_start(incident, request)

        _log_response_snapshot("CREATE", incident)

        return Response(
            {
                "status": "success",
                "code": 201,
                "message": "Incident created successfully",
                "data": IncidentSerializer(incident).data,
            },
            status=status.HTTP_201_CREATED,
        )


# =========================
# RETRIEVE / UPDATE / PATCH / DELETE
# =========================
class IncidentDetailAPIView(APIView):
    """
    Handles GET, PUT, PATCH, DELETE operations for a single incident.
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":    "incidents.view",
        "PUT":    "incidents.edit",
        "DELETE": "incidents.edit",
    }
    # PATCH: allow users with incidents.edit OR incidents.comment (comment-only path)
    allow_any_permissions = {
        "PATCH": ["incidents.edit", "incidents.comment"],
    }
    # Agents & Guardians can view incident details for their residents
    allow_portal_roles = True

    # -------------------------
    # helper: get incident by UUID with scope check
    # -------------------------
    def get_object(self, uuid, user=None):
        try:
            incident = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return None

        # Scope check: ASSIGNED_HOME users can only access incidents for their residents
        if user and not check_scope_access(
            user, incident, "incidents.view",
            group_home_path="resident__leads__group_home_assignments__group_home",
        ):
            return None

        return incident

    # -------------------------
    # GET: retrieve incident details
    # -------------------------
    @extend_schema(
        operation_id="get_incident",
        summary="Get incident details",
        tags=["Incident"],
    )
    def get(self, request, uuid):
        incident = self.get_object(uuid, user=request.user)
        if not incident:
            return Response({"message": "Incident not found"}, status=404)

        return Response({"status": "success", "data": IncidentSerializer(incident).data}, status=200)

    # -------------------------
    # PUT: full update of incident
    # -------------------------
    @extend_schema(
        operation_id="update_incident",
        summary="Update incident (full)",
        tags=["Incident"],
        request=IncidentSerializer,
    )
    def put(self, request, uuid):
        _log_request_snapshot("UPDATE", request, extra=f"uuid={uuid}")
        incident = self.get_object(uuid, user=request.user)
        if not incident:
            logger.warning(f"[sig-trace] UPDATE incident_not_found uuid={uuid}")
            return Response({"message": "Incident not found"}, status=404)

        # T24: core-field lockout
        if incident.status != "DRAFT":
            field_errors = _check_core_fields_unchanged(incident, request.data)
            if field_errors:
                return Response({"status": "error", "errors": field_errors}, status=400)

        # T23: capture pre-save snapshot for audit
        audit_before = {f: getattr(incident, f, None) for f in AUDIT_FIELDS}

        serializer = IncidentSerializer(incident, data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info(
            f"[sig-trace] UPDATE serializer.validated_data keys="
            f"{list(serializer.validated_data.keys())}"
        )

        medical_flags = serializer.validated_data.pop("medical_flags", [])
        legal_flags = serializer.validated_data.pop("legal_flags", [])
        social_flags = serializer.validated_data.pop("social_flags", [])
        victim_flags = serializer.validated_data.pop("victim_flags", [])
        notifications = request.data.get("notifications", [])
        signature_in_payload = "signature_media_id" in request.data
        # IMPORTANT: not a model field; must be removed before setattr loop
        signature_media_id = serializer.validated_data.pop("signature_media_id", None) if signature_in_payload else None
        duplicate_signature_from_profile = serializer.validated_data.pop(
            "duplicate_signature_from_profile", False
        )

        # Resolve new write-only FK inputs (group_home_uuid, assigned_program_manager_uuid).
        gh_uuid = serializer.validated_data.pop("group_home_uuid", None)
        if gh_uuid:
            from group_home.models import GroupHome
            try:
                serializer.validated_data["group_home"] = GroupHome.objects.get(uuid=gh_uuid)
            except GroupHome.DoesNotExist:
                return Response(
                    {"status": "error", "errors": {"group_home_uuid": "Group home not found"}},
                    status=400,
                )

        pm_uuid = serializer.validated_data.pop("assigned_program_manager_uuid", None)
        if pm_uuid:
            from accounts.models import User as _User
            try:
                serializer.validated_data["assigned_program_manager"] = _User.objects.get(uuid=pm_uuid)
            except _User.DoesNotExist:
                return Response(
                    {"status": "error", "errors": {"assigned_program_manager_uuid": "User not found"}},
                    status=400,
                )

        pm_signature_in_payload = "pm_signature_media_id" in request.data
        pm_signature_media_id = (
            serializer.validated_data.pop("pm_signature_media_id", None)
            if pm_signature_in_payload
            else None
        )


        logger.info(
            f"[sig-duplicate] UPDATE payload incident_uuid={getattr(incident, 'uuid', '')} "
            f"incident_id={getattr(incident, 'id', None)} "
            f"user_id={getattr(request.user, 'id', None)} "
            f"signature_in_payload={signature_in_payload} "
            f"signature_media_id={signature_media_id} "
            f"duplicate_signature_from_profile={duplicate_signature_from_profile}"
        )

        with transaction.atomic():
            # commit: update main fields
            for attr, value in serializer.validated_data.items():
                setattr(incident, attr, value)
            incident.save()

            # T23: write IncidentEdit row when state is non-DRAFT and any tracked field changed
            if incident.status != "DRAFT":
                field_changes = {}
                for f in AUDIT_FIELDS:
                    new_val = getattr(incident, f, None)
                    if audit_before[f] != new_val:
                        field_changes[f] = {"old": audit_before[f], "new": new_val}
                if field_changes:
                    IncidentEdit.objects.create(
                        incident=incident, edited_by=request.user, field_changes=field_changes,
                    )

            # ---------------- SIGNATURE (incident-scoped) ---------------- #
            try:
                content_type_incident = ContentType.objects.get(app_label="incidents", model="incident")

                if incident.reporter_signature_id and not signature_in_payload and not duplicate_signature_from_profile:
                    logger.info(
                        f"[sig-duplicate] UPDATE skip reporter signature mutation "
                        f"incident_id={incident.id} incident_uuid={incident.uuid} "
                        f"reporter_signature_id={incident.reporter_signature_id}"
                    )
                elif duplicate_signature_from_profile and not (
                    signature_in_payload and signature_media_id
                ):
                    logger.info(
                        f"[sig-duplicate] UPDATE branch=duplicate_from_profile "
                        f"incident_id={incident.id} incident_uuid={incident.uuid}"
                    )
                    source = _get_user_profile_signature_media(request.user)
                    if source:
                        new_media = _duplicate_media_to_incident(source, incident, request.user)
                        if new_media:
                            Media.objects.filter(
                                content_type=content_type_incident,
                                object_id=incident.id,
                                status="active",
                                alt_text__icontains="signature",
                            ).exclude(id=new_media.id).update(status="deleted")
                            # Bind the new explicit FK.
                            incident.reporter_signature = new_media
                            incident.save(update_fields=["reporter_signature", "updated_at"])
                    else:
                        logger.warning(
                            f"[sig-duplicate] UPDATE flag set but no profile signature "
                            f"user_id={getattr(request.user, 'id', None)}"
                        )
                elif signature_in_payload:
                    if signature_media_id is None or (isinstance(signature_media_id, str) and not signature_media_id.strip()):
                        Media.objects.filter(
                            content_type=content_type_incident,
                            object_id=incident.id,
                            status="active",
                            alt_text__icontains="signature",
                        ).update(status="deleted")
                        # Clear FK so /start sees missing signature.
                        incident.reporter_signature = None
                        incident.save(update_fields=["reporter_signature", "updated_at"])
                    else:
                        media_id = str(signature_media_id).strip()
                        media_obj = Media.objects.filter(id=media_id, status="active").first()
                        if media_obj:
                            media_obj.content_type = content_type_incident
                            media_obj.object_id = incident.id
                            if not (media_obj.alt_text and "signature" in media_obj.alt_text.lower()):
                                media_obj.alt_text = f"Incident {incident.uuid} signature"
                            media_obj.save()
                            # Bind the new explicit FK so /start sees the signature.
                            incident.reporter_signature = media_obj
                            incident.save(update_fields=["reporter_signature", "updated_at"])
                        Media.objects.filter(
                            content_type=content_type_incident,
                            object_id=incident.id,
                            status="active",
                            alt_text__icontains="signature",
                        ).exclude(id=media_id).update(status="deleted")
            except Exception as e:
                # never fail incident update due to signature linking
                logger.error(
                    f"[sig-duplicate] UPDATE signature handling FAILED "
                    f"incident_id={getattr(incident, 'id', None)} "
                    f"incident_uuid={getattr(incident, 'uuid', '')} "
                    f"user_id={getattr(request.user, 'id', None)} "
                    f"err={type(e).__name__}: {e}\n{traceback.format_exc()}"
                )

            # ---------------- PM / COORDINATOR SIGNATURE ---------------- #
            try:
                if pm_signature_in_payload and pm_signature_media_id:
                    content_type_incident = ContentType.objects.get(app_label="incidents", model="incident")
                    pm_media_id = str(pm_signature_media_id).strip()
                    pm_media_obj = Media.objects.filter(id=pm_media_id, status="active").first()
                    if not pm_media_obj:
                        raise ValueError("PM signature not found")
                    if incident.reporter_signature_id and str(pm_media_obj.id) == str(incident.reporter_signature_id):
                        raise ValueError("PM signature cannot reuse the reporter signature")

                    pm_media_obj.content_type = content_type_incident
                    pm_media_obj.object_id = incident.id
                    if not (pm_media_obj.alt_text and "signature" in pm_media_obj.alt_text.lower()):
                        pm_media_obj.alt_text = f"Incident {incident.uuid} PM signature"
                    pm_media_obj.save()
                    incident.pm_signature = pm_media_obj
                    incident.save(update_fields=["pm_signature", "updated_at"])
            except Exception as e:
                logger.error(
                    f"[sig-duplicate] UPDATE PM signature handling FAILED "
                    f"incident_id={getattr(incident, 'id', None)} "
                    f"incident_uuid={getattr(incident, 'uuid', '')} "
                    f"user_id={getattr(request.user, 'id', None)} "
                    f"err={type(e).__name__}: {e}\n{traceback.format_exc()}"
                )
                return Response(
                    {"status": "error", "errors": {"pm_signature_media_id": str(e)}},
                    status=400,
                )

            # commit: delete existing flags and recreate
            incident.medical_flags.all().delete()
            incident.legal_flags.all().delete()
            incident.social_flags.all().delete()
            incident.victim_flags.all().delete()

            for flag in medical_flags:
                IncidentMedicalFlag.objects.create(incident=incident, **flag)
            for flag in legal_flags:
                IncidentLegalFlag.objects.create(incident=incident, **flag)
            for flag in social_flags:
                IncidentSocialFlag.objects.create(incident=incident, **flag)
            for flag in victim_flags:
                IncidentVictimFlag.objects.create(incident=incident, **flag)
            

        resident = incident.resident
        lead = Lead.objects.filter(user=resident).first()

        guardian_user = lead.guardian if lead else None
        agent_user = lead.agent if lead else None

        # Delete existing notifications and recreate from request
        incident.incident_notifications.all().delete()

        for raw in notifications:
            notify_type = _normalize_notification_type(raw)
            if notify_type is None:
                continue

            user = _notification_user_for_type(
                incident,
                notify_type,
                lead,
                request.user,
            )

            # Extract notify_date / notify_time / method_of_contact / by_whom from payload (if dict)
            raw_notify_date = None
            raw_notify_time = None
            raw_method_of_contact = None
            raw_by_whom = None
            if isinstance(raw, dict):
                raw_notify_date = raw.get("notify_date") or None
                raw_notify_time = raw.get("notify_time") or None
                raw_method_of_contact = raw.get("method_of_contact") or None
                raw_by_whom = raw.get("by_whom") or None

            IncidentNotification.objects.create(
                incident=incident,
                type=notify_type,
                user=user,
                notify=True,
                notify_date=raw_notify_date,
                notify_time=raw_notify_time,
                method_of_contact=raw_method_of_contact,
                by_whom=raw_by_whom,
            )
            
            log_action(
                request=request,
                action="UPDATE",
                entity_type="Incident",
                entity_id=str(incident.uuid),
                group_home=getattr(incident, "group_home", None),
                message="Incident updated",
            )

        # ---------------- COMMENT (inline) ---------------- #
        comment_text = request.data.get("comment")
        if comment_text:
            user_role = getattr(request.user, "role", None)
            IncidentComment.objects.create(
                incident=incident,
                comment=comment_text,
                created_by=request.user,
                role=user_role,
            )

        # Auto-start: if a DSP edit lands a draft into a fully-startable
        # state, transition silently to IN_PROGRESS so the user doesn't
        # need to make a second API call. Matches the spec rule
        # "when the incident is started, the status should change
        # automatically".
        _maybe_auto_start(incident, request)

        _log_response_snapshot("UPDATE", incident)

        return Response(
            {"status": "success", "message": "Incident updated successfully", "data": IncidentSerializer(incident).data},
            status=200,
        )

    # -------------------------
    # PATCH: partial update of incident fields
    # -------------------------
    @extend_schema(
        operation_id="patch_incident",
        summary="Partial update of incident",
        tags=["Incident"],
    )
    def patch(self, request, uuid):
        incident = self.get_object(uuid, user=request.user)
        if not incident:
            return Response(
                {"status": "error", "message": "Incident not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # T24: core-field lockout
        if incident.status != "DRAFT":
            field_errors = _check_core_fields_unchanged(incident, request.data)
            if field_errors:
                return Response({"status": "error", "errors": field_errors}, status=400)

        # T23: capture pre-save snapshot for audit
        audit_before = {f: getattr(incident, f, None) for f in AUDIT_FIELDS}

        # Determine if user is allowed to edit incident fields (vs. comment-only)
        can_edit = has_permission(request.user, "incidents.edit") is not None

        if can_edit:
            serializer = IncidentSerializer(
                incident,
                data=request.data,
                partial=True
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()

            incident.refresh_from_db()
            if incident.status != "DRAFT":
                field_changes = {}
                for f in AUDIT_FIELDS:
                    new_val = getattr(incident, f, None)
                    if audit_before[f] != new_val:
                        field_changes[f] = {"old": audit_before[f], "new": new_val}
                if field_changes:
                    IncidentEdit.objects.create(
                        incident=incident, edited_by=request.user, field_changes=field_changes,
                    )

        # ---------------- COMMENT (inline) ---------------- #
        # Allowed for both incidents.edit and incidents.comment holders
        comment_text = request.data.get("comment")
        if comment_text:
            user_role = getattr(request.user, "role", None)
            IncidentComment.objects.create(
                incident=incident,
                comment=comment_text,
                created_by=request.user,
                role=user_role,
            )

        log_action(
            request=request,
            action="UPDATE",
            entity_type="Incident",
            entity_id=str(incident.uuid),
            group_home=getattr(incident, "group_home", None),
            message="Incident partially updated" if can_edit else "Comment added to incident",
        )

        if can_edit:
            _maybe_auto_start(incident, request)

        return Response(
            {
                "status": "success",
                "message": "Incident updated successfully" if can_edit else "Comment added successfully",
                "data": IncidentSerializer(incident).data,
            },
            status=status.HTTP_200_OK,
        )

    # -------------------------
    # DELETE: soft delete
    # -------------------------
    @extend_schema(
        operation_id="delete_incident",
        summary="Delete incident",
        tags=["Incident"],
    )
    def delete(self, request, uuid):
        incident = self.get_object(uuid, user=request.user)
        if not incident:
            return Response({"message": "Incident not found"}, status=404)

        incident.deleted_at = timezone.now()
        incident.save(update_fields=["deleted_at"])

        log_action(
                request=request,
                action="DELETE",
                entity_type="Incident",
                entity_id=str(incident.uuid),
                group_home=getattr(incident, "group_home", None),
                message="Incident deleted",
            )


        return Response({"status": "success", "message": "Incident deleted successfully"}, status=200)


# =========================
# PATCH: update only incident status
# =========================
class IncidentStatusUpdateAPIView(APIView):
    """
    Handles PATCH for updating the 'status' field only (Close / Acknowledge Incident).
    Staff need incidents.close permission.
    Guardians & Agents (portal roles) may only set status=ACKNOWLEDGED.
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PATCH": "incidents.close",
    }
    # Let portal roles (GUARDIAN / AGENT) through the permission gate for PATCH.
    # Their allowed status values are restricted below.
    allow_portal_roles = {"PATCH"}

    PORTAL_ROLE_TYPES = {"AGENT", "GUARDIAN", "RESIDENT", "LEAD"}

    @extend_schema(
        operation_id="update_incident_status",
        summary="Update incident status",
        tags=["Incident"],
    )
    def patch(self, request, uuid):
        try:
            incident = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return Response({"message": "Incident not found"}, status=404)

        user_role_type = getattr(getattr(request.user, "role", None), "type", None)
        is_portal_role = user_role_type in self.PORTAL_ROLE_TYPES

        # Portal roles (guardian / agent) may only acknowledge incidents.
        if is_portal_role:
            new_status = request.data.get("status", "")
            if new_status != Incident.Status.ACKNOWLEDGED:
                return Response(
                    {"message": "You can only acknowledge incidents."},
                    status=403,
                )
            # Verify the incident belongs to their resident (basic ownership check)
            # Guardian: resident must be linked to their guardian account
            # Agent: resident must be linked to their agent account
            if user_role_type == "GUARDIAN":
                linked = Lead.objects.filter(
                    user=incident.resident,
                    guardian=request.user,
                ).exists()
            elif user_role_type == "AGENT":
                linked = Lead.objects.filter(
                    user=incident.resident,
                    agent=request.user,
                ).exists()
            else:
                linked = False

            if not linked:
                return Response({"message": "Incident not found"}, status=404)

            incident.status = Incident.Status.ACKNOWLEDGED
            incident.save(update_fields=["status", "updated_at"])

            # Save optional comment
            comment_text = request.data.get("comment")
            if comment_text:
                user_role = getattr(request.user, "role", None)
                IncidentComment.objects.create(
                    incident=incident,
                    comment=comment_text,
                    created_by=request.user,
                    role=user_role,
                )

            log_action(
                request=request,
                action="UPDATE",
                entity_type="Incident",
                entity_id=str(incident.uuid),
                group_home=getattr(incident, "group_home", None),
                message="Incident acknowledged by portal user",
            )

            response = Response(
                {"status": "success", "message": "Incident acknowledged successfully", "data": {"status": incident.status}},
                status=200,
            )
            response["Sunset"] = "Wed, 30 Jul 2026 00:00:00 GMT"
            response["Deprecation"] = "true"
            response["Link"] = '</api/incidents/{uuid}/acknowledge/>; rel="successor-version"'
            return response

        # --- Staff path (requires incidents.close permission via HasPermission) ---

        # Scope check: ASSIGNED_HOME users can only close incidents for their residents
        if not check_scope_access(
            request.user, incident, "incidents.close",
            group_home_path="resident__leads__group_home_assignments__group_home",
        ):
            return Response({"message": "Incident not found"}, status=404)

        if "status" not in request.data:
            return Response({"message": "status field is required"}, status=400)

        incident.status = request.data["status"]
        incident.save(update_fields=["status", "updated_at"])

        log_action(
                request=request,
                action="UPDATE",
                entity_type="Incident",
                entity_id=str(incident.uuid),
                group_home=getattr(incident, "group_home", None),
                message=f"Incident status changed to {incident.status}",
            )

        response = Response(
            {"status": "success", "message": "Incident status updated successfully", "data": {"status": incident.status}},
            status=200,
        )
        response["Sunset"] = "Wed, 30 Jul 2026 00:00:00 GMT"
        response["Deprecation"] = "true"
        response["Link"] = '</api/incidents/{uuid}/start/>; rel="successor-version"'
        return response


# =========================
# POST /api/incidents/{uuid}/start/
# =========================
from incidents.transitions import (
    validate_start,
    ALLOWED_TRANSITIONS,
)


class IncidentStartAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"POST": "incidents.start"}

    def post(self, request, uuid):
        try:
            inc = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return Response({"status": "error", "message": "Incident not found"}, status=404)

        errors = validate_start(inc)
        if errors and inc.status != "DRAFT":
            return Response(
                {
                    "current_status": inc.status,
                    "allowed_transitions": ALLOWED_TRANSITIONS.get(inc.status, []),
                    "error": errors[0],
                },
                status=409,
            )
        if errors:
            return Response({"errors": errors}, status=422)

        with transaction.atomic():
            now = timezone.now()
            # New flow: DRAFT goes straight to PM_REVIEW_PENDING. IN_PROGRESS
            # is no longer a normal intermediate state.
            inc.status = "PM_REVIEW_PENDING"
            inc.started_at = now
            inc.submitted_for_review_at = now
            if inc.assigned_program_manager and not inc.program_manager_email:
                inc.program_manager_email = inc.assigned_program_manager.email
            if inc.program_manager_email and not inc.coordinator_email:
                inc.coordinator_email = inc.program_manager_email
            inc.save(update_fields=[
                "status", "started_at", "submitted_for_review_at",
                "program_manager_email", "coordinator_email",
                "updated_at",
            ])

            # Hand-off notification for the assigned PM. Record the actor
            # (typically the DSP starting the incident) and email method.
            if inc.assigned_program_manager_id:
                actor_name = (
                    f"{request.user.first_name or ''} {request.user.last_name or ''}".strip()
                    or getattr(request.user, "email", "")
                )
                already = IncidentNotification.objects.filter(
                    incident=inc, type="PROGRAM_MANAGER",
                    user=inc.assigned_program_manager,
                ).exists()
                if not already:
                    IncidentNotification.objects.create(
                        incident=inc, type="PROGRAM_MANAGER",
                        user=inc.assigned_program_manager, notify=True,
                        notify_date=now.date(), notify_time=now.time(),
                        method_of_contact="EMAIL", by_whom=actor_name,
                    )

            log_action(
                request=request, action="STATUS_CHANGE", entity_type="Incident",
                entity_id=str(inc.uuid),
                group_home=inc.group_home,
                message=f"DRAFT → PM_REVIEW_PENDING by {request.user.email}",
            )
        return Response(
            {"status": "success", "data": {"status": inc.status, "started_at": inc.started_at}},
            status=200,
        )


# =========================
# POST /api/incidents/{uuid}/submit-for-review/
# =========================
from incidents.transitions import validate_submit_for_review


class IncidentSubmitForReviewAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"POST": "incidents.submit_for_review"}

    def post(self, request, uuid):
        try:
            inc = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return Response({"status": "error", "message": "Incident not found"}, status=404)

        errors = validate_submit_for_review(inc)
        if errors and inc.status != "IN_PROGRESS":
            return Response(
                {
                    "current_status": inc.status,
                    "allowed_transitions": ALLOWED_TRANSITIONS.get(inc.status, []),
                    "error": errors[0],
                },
                status=409,
            )
        if errors:
            return Response({"errors": errors}, status=422)

        with transaction.atomic():
            inc.status = "PM_REVIEW_PENDING"
            inc.submitted_for_review_at = timezone.now()
            inc.save(update_fields=["status", "submitted_for_review_at", "updated_at"])

            now = timezone.now()
            actor_name = (
                f"{request.user.first_name or ''} {request.user.last_name or ''}".strip()
                or getattr(request.user, "email", "")
            )
            IncidentNotification.objects.create(
                incident=inc, type="PROGRAM_MANAGER",
                user=inc.assigned_program_manager, notify=True,
                notify_date=now.date(),
                notify_time=now.time(),
                method_of_contact="EMAIL", by_whom=actor_name,
            )

            log_action(
                request=request, action="STATUS_CHANGE", entity_type="Incident",
                entity_id=str(inc.uuid), group_home=inc.group_home,
                message=f"IN_PROGRESS → PM_REVIEW_PENDING by {request.user.email}",
            )

        return Response(
            {
                "status": "success",
                "data": {
                    "status": inc.status,
                    "submitted_for_review_at": inc.submitted_for_review_at,
                },
            },
            status=200,
        )


# =========================
# POST /api/incidents/{uuid}/send-back/
# =========================
from incidents.transitions import validate_send_back


class IncidentSendBackAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"POST": "incidents.pm_review"}

    def post(self, request, uuid):
        try:
            inc = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return Response({"status": "error", "message": "Incident not found"}, status=404)

        reason = (request.data.get("reason") or "").strip()

        # Two-layer auth: must be assigned PM OR Admin/Director
        role_name = getattr(getattr(request.user, "role", None), "name", "")
        is_admin_like = request.user.is_superuser or role_name in ("Admin", "Program Director")
        is_pm_role = role_name in ("Program Manager", "Program Coordinator")
        if not (is_admin_like or is_pm_role or request.user.id == inc.assigned_program_manager_id):
            return Response(
                {"error": "Only a Program Manager or Program Coordinator can send this back"},
                status=403,
            )

        errors = validate_send_back(inc, reason)
        if errors and inc.status != "PM_REVIEW_PENDING":
            return Response(
                {
                    "current_status": inc.status,
                    "allowed_transitions": ALLOWED_TRANSITIONS.get(inc.status, []),
                    "error": errors[0],
                },
                status=409,
            )
        if errors:
            return Response({"errors": {"reason": errors[0]}}, status=400)

        with transaction.atomic():
            inc.status = "DRAFT"
            inc.save(update_fields=["status", "updated_at"])
            IncidentComment.objects.create(
                incident=inc, comment=f"[Send back] {reason}",
                created_by=request.user, role=getattr(request.user, "role", None),
            )
            log_action(
                request=request, action="STATUS_CHANGE", entity_type="Incident",
                entity_id=str(inc.uuid), group_home=inc.group_home,
                message=f"PM_REVIEW_PENDING → DRAFT (send-back) by {request.user.email}",
            )

        return Response({"status": "success", "data": {"status": inc.status}}, status=200)


# =========================
# POST /api/incidents/{uuid}/pm-signoff/
# =========================
from incidents.transitions import validate_pm_signoff


class IncidentPMSignoffAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"POST": "incidents.pm_review"}

    def post(self, request, uuid):
        try:
            inc = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return Response({"status": "error", "message": "Incident not found"}, status=404)

        # Idempotency: if already COMPLETED, return current state without side effects
        if inc.status == "COMPLETED":
            return Response(
                {
                    "status": "success",
                    "idempotent": True,
                    "data": {"status": inc.status, "completed_at": inc.completed_at},
                },
                status=200,
            )

        # Two-layer auth
        role_name = getattr(getattr(request.user, "role", None), "name", "")
        is_admin = request.user.is_superuser or role_name in ("Admin", "Program Director")
        is_pm_role = role_name in ("Program Manager", "Program Coordinator")
        if not (is_admin or is_pm_role or request.user.id == inc.assigned_program_manager_id):
            return Response(
                {"error": "Only a Program Manager or Program Coordinator can sign off on this incident"},
                status=403,
            )

        media_id = request.data.get("pm_signature_media_id")
        errors = validate_pm_signoff(inc, media_id)
        if errors and inc.status != "PM_REVIEW_PENDING":
            return Response(
                {
                    "current_status": inc.status,
                    "allowed_transitions": ALLOWED_TRANSITIONS.get(inc.status, []),
                    "error": errors[0],
                },
                status=409,
            )
        if errors:
            return Response({"errors": {"pm_signature_media_id": errors[0]}}, status=400)

        media_obj = (
            Media.objects.filter(id=media_id, status="active").first()
            if media_id
            else inc.pm_signature
        )
        if not media_obj and not media_id:
            source = _get_user_profile_signature_media(request.user)
            if source:
                media_obj = _duplicate_media_to_incident(source, inc, request.user)
        if not media_obj:
            return Response(
                {"errors": {"pm_signature_media_id": "Signature not found"}},
                status=400,
            )
        if inc.reporter_signature_id and str(media_obj.id) == str(inc.reporter_signature_id):
            return Response(
                {"errors": {"pm_signature_media_id": "Reviewer signature cannot reuse the reporter signature"}},
                status=400,
            )

        with transaction.atomic():
            inc.status = "COMPLETED"
            inc.completed_at = timezone.now()
            inc.pm_signature = media_obj

            # Re-target the media at this incident
            ct = ContentType.objects.get(app_label="incidents", model="incident")
            if media_obj.object_id != inc.id or media_obj.content_type_id != ct.id:
                media_obj.content_type = ct
                media_obj.object_id = inc.id
                if not (media_obj.alt_text and "signature" in (media_obj.alt_text or "").lower()):
                    media_obj.alt_text = f"Incident {inc.uuid} PM signature"
                media_obj.save()
            inc.save(update_fields=["status", "completed_at", "pm_signature", "updated_at"])

            # FIRE-AND-FORGET COMPLETION EMAILS
            # (Notification records for Guardian/Agent are no longer created here as per user request,
            # but emails are still sent below).

            log_action(
                request=request, action="STATUS_CHANGE", entity_type="Incident",
                entity_id=str(inc.uuid), group_home=inc.group_home,
                message=f"PM_REVIEW_PENDING → COMPLETED by {request.user.email}",
            )

            # Fire-and-forget completion emails so the HTTP response returns
            # immediately. SendGrid SMTP can take several seconds locally and
            # even longer in production — there's no reason to block PM
            # sign-off on it.
            import threading
            from incidents.emails import send_completion_email

            lead = Lead.objects.filter(user=inc.resident).first() if inc.resident_id else None
            recipients_for_email = []
            if lead:
                if lead.guardian_id:
                    recipients_for_email.append(lead.guardian)
                if lead.agent_id:
                    recipients_for_email.append(lead.agent)

            recipients_tuple = tuple(recipients_for_email)
            incident_for_email = inc
            signed_off_by_user = request.user

            def _send_completion_emails():
                # NB: this runs in a worker thread *after* the DB commit. We
                # close the thread-local connection on exit so we don't leak.
                try:
                    for recipient in recipients_tuple:
                        try:
                            send_completion_email(
                                incident_for_email,
                                recipient,
                                signed_off_by_user=signed_off_by_user,
                            )
                        except Exception as e:
                            logger.warning(
                                f"[email] async send failed incident={incident_for_email.uuid} "
                                f"to={getattr(recipient, 'email', '?')} err={type(e).__name__}: {e}"
                            )
                finally:
                    try:
                        from django.db import connection
                        connection.close()
                    except Exception:
                        pass

            def _spawn_email_thread():
                threading.Thread(
                    target=_send_completion_emails,
                    name="incident-completion-email",
                    daemon=True,
                ).start()

            transaction.on_commit(_spawn_email_thread)


        return Response(
            {"status": "success", "data": {"status": inc.status, "completed_at": inc.completed_at}},
            status=200,
        )


# =========================
# POST /api/incidents/{uuid}/acknowledge/
# =========================
from incidents.transitions import validate_acknowledge


class IncidentAcknowledgeAPIView(APIView):
    """Portal acknowledge endpoint — Guardian or Agent linked to the resident."""
    permission_classes = [IsAuthenticated]
    PORTAL_ROLE_TYPES = {"GUARDIAN", "AGENT"}

    def post(self, request, uuid):
        try:
            inc = Incident.objects.get(uuid=uuid, deleted_at__isnull=True)
        except Incident.DoesNotExist:
            return Response({"status": "error", "message": "Incident not found"}, status=404)

        # Idempotency: already acknowledged
        if inc.status == "ACKNOWLEDGED":
            return Response(
                {
                    "status": "success", "idempotent": True,
                    "data": {"status": inc.status},
                },
                status=200,
            )

        role_type = getattr(getattr(request.user, "role", None), "type", None)
        if role_type not in self.PORTAL_ROLE_TYPES:
            return Response({"error": "Only Guardian or Agent can acknowledge"}, status=403)

        if role_type == "GUARDIAN":
            linked = Lead.objects.filter(user=inc.resident, guardian=request.user).exists()
        else:  # AGENT
            linked = Lead.objects.filter(user=inc.resident, agent=request.user).exists()
        if not linked:
            return Response({"status": "error", "message": "Incident not found"}, status=404)

        errors = validate_acknowledge(inc)
        if errors:
            return Response(
                {
                    "current_status": inc.status,
                    "allowed_transitions": ALLOWED_TRANSITIONS.get(inc.status, []),
                    "error": errors[0],
                },
                status=409,
            )

        with transaction.atomic():
            inc.status = "ACKNOWLEDGED"
            inc.save(update_fields=["status", "updated_at"])
            log_action(
                request=request, action="STATUS_CHANGE", entity_type="Incident",
                entity_id=str(inc.uuid), group_home=inc.group_home,
                message=f"COMPLETED → ACKNOWLEDGED by {role_type} {request.user.email}",
            )
        return Response({"status": "success", "data": {"status": inc.status}}, status=200)


# =========================
# GET /api/incidents/resident-context/{resident_uuid}/
# =========================
class IncidentResidentContextAPIView(APIView):
    """
    Returns the Group Home + Program Manager + DSPs already linked to a
    resident through onboarding. Lets the incident form auto-populate
    instead of asking the user to re-select fields the system already knows.
    """
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {"GET": "incidents.create"}

    def get(self, request, resident_uuid):
        try:
            resident = User.objects.get(uuid=resident_uuid, deleted_at__isnull=True)
        except User.DoesNotExist:
            return Response({"status": "error", "message": "Resident not found"}, status=404)

        from incidents.context import resident_incident_context, serialize_context
        ctx = resident_incident_context(resident)
        payload = serialize_context(ctx)
        payload["resident"] = {
            "uuid": str(resident.uuid),
            "first_name": resident.first_name,
            "last_name": resident.last_name,
        }
        return Response({"status": "success", "data": payload}, status=200)
