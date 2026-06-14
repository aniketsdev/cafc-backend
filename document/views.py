import base64
import binascii
import logging
import os

from rest_framework.views import APIView
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status, serializers
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated, AllowAny
from accounts.permissions import HasPermission, has_permission as check_perm

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from document.models import ConsentForm, ConsentFormEntry
from document.serializers import (
    ConsentFormSerializer,
    ConsentFormEntrySerializer,
    ConsentFormCreateSerializer,
    ConsentFormDetailSerializer,
    ConsentFormUpdateSerializer,
    ConsentFormShareSerializer,
)
from leads.models import Lead
from leads.status import recompute_and_save
from media.models import Media
from accounts.email_service import send_email

logger = logging.getLogger(__name__)

class ConsentFormCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    # No class-level HasPermission: nurse needs to reach the inline form_code
    # check before being blocked. Non-nurse users are checked inline below.

    @extend_schema(
    operation_id="create_consent_form",
    summary="Create consent form",
    description="Create a consent form entry with versioning and next due date logic.",
    tags=["Consent Forms"],
    request=ConsentFormCreateSerializer,
    responses={201: ConsentFormEntrySerializer},
)
    def post(self, request):
        try:
            data = request.data

            # -----------------------------------------------------------------
            # Inline RBAC: Nurses may only fill the nursing transition form.
           
            # -----------------------------------------------------------------
            user_role_name = getattr(getattr(request.user, "role", None), "name", "")
            user_role_type = (getattr(getattr(request.user, "role", None), "type", "") or "").upper()
            NURSE_FORM_CODE = "5_AND_30_DAY_NURSING_TRANSITION_EVALUATION_FORM"

            if user_role_name == "Nurse":
                # Nurse: only the nursing transition form is allowed
                if data.get("form_code") != NURSE_FORM_CODE:
                    return Response(
                        {
                            "status": "error",
                            "code": status.HTTP_403_FORBIDDEN,
                            "message": "Nurses may only fill the nursing transition evaluation form.",
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )
            elif user_role_type in ("GUARDIAN", "AGENT"):
                # Portal users (G/A) are allowed — they fill forms assigned to them
                pass
            else:
                # All other staff must have consent_forms.create permission
                if not check_perm(request.user, "consent_forms.create"):
                    return Response(
                        {
                            "status": "error",
                            "code": status.HTTP_403_FORBIDDEN,
                            "message": "You do not have permission to fill consent forms.",
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )

            # Validate required fields up front so a missing/empty body returns
            # a structured 400 instead of crashing on direct key access below.
            serializer = ConsentFormCreateSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data

            resident = Lead.objects.get(uuid=data["resident_uuid"])

            signer_type = data.get("signer_type")

            with transaction.atomic():

                lookup = {
                    "resident": resident,
                    "form_code": data["form_code"],
                    "deleted_at__isnull": True,
                }
                if signer_type:
                    lookup["signer_type"] = signer_type

                form, _ = ConsentForm.objects.get_or_create(
                    **lookup,
                    defaults={
                        "form_name": data["form_name"],
                        "frequency_type": data["frequency_type"],
                        **({"signer_type": signer_type} if signer_type else {}),
                    },
                )

                # ---------- VERSION LOGIC ----------
                last_entry = form.entries.order_by("-created_at").first()
                version = f"v{int(last_entry.version[1:]) + 1}" if last_entry else "v1"

                # ---------- NEXT DUE DATE ----------
                next_due_at = data.get("next_due_at")
                
                status_value = data["status"]

                filled_at = None
                if status_value == "COMPLETED":
                    filled_at = timezone.now()

                entry = ConsentFormEntry.objects.create(
                    form=form,
                    version=version,
                    status=data["status"],
                    filled_at=filled_at,
                    next_due_at=next_due_at,
                    form_json=data.get("form_json", {}),
                )

                # Recompute lead status after consent form creation
                recompute_and_save(resident, request=request)

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_201_CREATED,
                    "message": "Consent form created successfully",
                    "data": ConsentFormEntrySerializer(entry).data,
                },
                status=status.HTTP_201_CREATED,
            )
            
        except Lead.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Resident not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except serializers.ValidationError as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": e.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Consent form creation failed",
                    "details": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
class ConsentFormDetailAPIView(APIView):

    permission_classes = [IsAuthenticated]
    #   GET    — open to authenticated users; inline nurse/G-A filter
    #   PUT    — non-nurse requires consent_forms.edit; nurse limited to nursing form
    #   DELETE — requires consent_forms.delete for ALL (nurse has none → blocked)
    allow_portal_roles = {"GET"}  # kept for symmetry; no HasPermission class active
    @extend_schema(
        operation_id="get_consent_form_detail",
        summary="Get consent form details",
        tags=["Consent Forms"],
        parameters=[
            OpenApiParameter(
                name="history",
                type=OpenApiTypes.BOOL,
                description="Include full history",
                required=False,
                default=False,
            )
        ],
    )
    def get(self, request, uuid):
        try:
            form = ConsentForm.objects.get(
                uuid=uuid,
                deleted_at__isnull=True
            )

            # RBAC: Guardian/Agent users can only access forms
            # assigned to their role.
            user_role_type = ""
            user_role_name = ""
            if hasattr(request.user, "role") and request.user.role:
                user_role_type = getattr(request.user.role, "type", "").upper()
                user_role_name = getattr(request.user.role, "name", "").strip()

            if (
                user_role_type in ("GUARDIAN", "AGENT")
                and form.signer_type
                and user_role_type not in {
                    signer.strip()
                    for signer in form.signer_type.split(",")
                    if signer.strip()
                }
            ):
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "You are not authorized to access this document.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Nurse: only the nursing transition form is accessible
            NURSE_FORM_CODE = "5_AND_30_DAY_NURSING_TRANSITION_EVALUATION_FORM"
            if user_role_name == "Nurse" and form.form_code != NURSE_FORM_CODE:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "Nurses may only access the nursing transition evaluation form.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            history = request.query_params.get("history", "false") == "true"

            entries_qs = form.entries.filter(
                deleted_at__isnull=True
            ).order_by("-created_at")

            if not history:
                entries_qs = entries_qs[:1]

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Consent form fetched successfully",
                    "data": {
                        "form": ConsentFormSerializer(form).data,
                        "entries": ConsentFormEntrySerializer(entries_qs, many=True).data,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except ConsentForm.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Consent form not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

    @extend_schema(
        operation_id="update_consent_form",
        summary="Update consent form",
        description=(
            "Update a consent form using form UUID. "
            "If latest entry is DRAFT → update same entry. "
            "If latest entry is COMPLETED → create new version."
        ),
        tags=["Consent Forms"],
        request=OpenApiTypes.OBJECT,
    )
    def put(self, request, uuid):
        try:
            serializer = ConsentFormUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            status_value = serializer.validated_data["status"]
            form_json = serializer.validated_data.get("form_json", {})
            next_due_at = serializer.validated_data.get("next_due_at")


            form = ConsentForm.objects.get(
                uuid=uuid,
                deleted_at__isnull=True
            )

            # Nurse: only the nursing transition form is editable
            user_role_name = getattr(getattr(request.user, "role", None), "name", "").strip()
            NURSE_FORM_CODE = "5_AND_30_DAY_NURSING_TRANSITION_EVALUATION_FORM"
            if user_role_name == "Nurse" and form.form_code != NURSE_FORM_CODE:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "Nurses may only edit the nursing transition evaluation form.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            latest_entry = (
                form.entries
                .filter(deleted_at__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if status_value not in {"DRAFT", "COMPLETED", "SIGNED"}:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Invalid status. Allowed values: DRAFT, COMPLETED, SIGNED",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            with transaction.atomic():

                # -----------------------------
                # CASE 1: No active entry → create next version.
                # Version numbering continues across soft-deleted entries so
                # a delete + refill becomes v2 (not v1 again) and re-shares
                # appear as a new version in the guardian/agent portal.
                # -----------------------------
                if not latest_entry:
                    last_any_entry = (
                        form.entries.order_by("-created_at").first()
                    )
                    try:
                        next_version = (
                            f"v{int(last_any_entry.version[1:]) + 1}"
                            if last_any_entry
                            else "v1"
                        )
                    except (TypeError, ValueError):
                        next_version = "v1"
                    entry = ConsentFormEntry.objects.create(
                        form=form,
                        version=next_version,
                        status=status_value,
                        form_json=form_json,
                        next_due_at=next_due_at,
                        filled_at=timezone.now()
                        if status_value == "COMPLETED"
                        else None,
                    )

                # -----------------------------
                # CASE 2: Latest is DRAFT
                # -----------------------------
                elif latest_entry.status == "DRAFT":
                    latest_entry.status = status_value
                    latest_entry.form_json = form_json
                    latest_entry.next_due_at = next_due_at

                    if status_value == "COMPLETED":
                        latest_entry.filled_at = timezone.now()

                    latest_entry.updated_at = timezone.now()
                    latest_entry.save()

                    entry = latest_entry

                # -----------------------------
                # CASE 3: Latest is COMPLETED
                # -----------------------------
                else:
                    last_version_number = int(latest_entry.version[1:])
                    new_version = f"v{last_version_number + 1}"

                    entry = ConsentFormEntry.objects.create(
                        form=form,
                        version=new_version,
                        status=status_value,
                        form_json=form_json,
                        next_due_at=next_due_at,
                        filled_at=timezone.now()
                        if status_value == "COMPLETED"
                        else None,
                    )

                # Bump the parent form's updated_at so the list API reflects
                # the edit. The share flow compares updated_at > shared_at to
                # decide whether the form changed since the last share, so
                # without this an edited form could never be re-shared.
                form.updated_at = timezone.now()
                form.save(update_fields=["updated_at"])

                # Recompute lead status after consent form update
                recompute_and_save(form.resident, request=request)

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Consent form updated successfully",
                    "data": {
                        "form_uuid": str(form.uuid),
                        "entry_id": entry.id,
                        "version": entry.version,
                        "status": entry.status,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except ConsentForm.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Consent form not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except serializers.ValidationError as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": e.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Consent form update failed",
                    "details": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(
        operation_id="delete_consent_form",
        summary="Delete consent form (soft delete)",
        tags=["Consent Forms"],
    )
    def delete(self, request, uuid):
        try:
            with transaction.atomic():
                form = ConsentForm.objects.get(
                    uuid=uuid,
                    deleted_at__isnull=True
                )

                # Get latest non-deleted entry
                latest_entry = (
                    form.entries
                    .filter(deleted_at__isnull=True)
                    .order_by("-created_at")
                    .first()
                )

                if not latest_entry:
                    return Response(
                        {
                            "status": "error",
                            "code": status.HTTP_400_BAD_REQUEST,
                            "message": "No active version found to delete",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                now = timezone.now()

                # 1️⃣ Soft delete ONLY latest entry
                latest_entry.deleted_at = now
                latest_entry.updated_at = now
                latest_entry.save(update_fields=["deleted_at", "updated_at"])

                # 2️⃣ Reset shared_at on parent ConsentForm so that if the user
                # re-fills the same form, the Share button is enabled again.
                if form.shared_at is not None:
                    form.shared_at = None
                    form.updated_at = now
                    form.save(update_fields=["shared_at", "updated_at"])

                # 3️⃣ Previously-shared Media records are intentionally KEPT.
                # Each shared PDF is a point-in-time snapshot; after a
                # delete + refill + re-share the portal shows the old and new
                # copies as Version 1 / Version 2 instead of losing history.

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Latest consent form version deleted successfully",
                    "data": {
                        "form_uuid": str(form.uuid),
                        "deleted_version": latest_entry.version,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except ConsentForm.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Consent form not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Consent form delete failed",
                    "details": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


#---------------------------------------------------------------------------------------------------

#---------------------------------------------------------------------------------------------------
class ConsentFormListAPIView(APIView):
    permission_classes = [IsAuthenticated]
    # No class-level HasPermission: inline RBAC fully handles role filtering:
    #   - Guardian/Agent → filtered to their signer_type forms
    #   - Nurse          → filtered to nursing transition form only
    #   - Staff          → see all forms (ASSIGNED_HOME scope handled inline)
    allow_portal_roles = {"GET"}  # informational only; no HasPermission class active
    @extend_schema(
        operation_id="list_consent_forms",
        summary="List consent forms",
        tags=["Consent Forms"],
        parameters=[
            OpenApiParameter("resident_uuid", OpenApiTypes.UUID),
            OpenApiParameter("search", OpenApiTypes.STR),
            OpenApiParameter("signer_type", OpenApiTypes.STR),
        ],
    )
    def get(self, request):
        qs = ConsentForm.objects.filter(deleted_at__isnull=True)

        resident_uuid = request.query_params.get("resident_uuid")
        search = request.query_params.get("search", "").strip()
        signer_type = request.query_params.get("signer_type", "").strip()

        if resident_uuid:
            qs = qs.filter(resident__uuid=resident_uuid)

        if search:
            qs = qs.filter(form_name__icontains=search)

        if signer_type:
            qs = qs.filter(signer_type=signer_type)

        # ----------------------------------
        # RBAC: Auto-filter by user role
        # ----------------------------------
        # Guardian and Agent users should only see consent forms
        # assigned to their role, regardless of query params.
        user_role_type = ""
        user_role_name = ""
        if hasattr(request.user, "role") and request.user.role:
            user_role_type = getattr(request.user.role, "type", "").upper()
            user_role_name = getattr(request.user.role, "name", "").strip()

        if user_role_type in ("GUARDIAN", "AGENT"):
            qs = qs.filter(
                Q(signer_type__icontains=user_role_type) | Q(signer_type__isnull=True)
            )
            # SECURITY: scope to THIS portal user's own residents. The
            # signer_type filter above only limits forms to the guardian/agent
            # *kind* — without the linkage filter below a guardian sees every
            # resident's guardian-signable forms (cross-tenant PHI leak).
            # ConsentForm.resident is a Lead, so guardian/agent live on it.
            if user_role_type == "GUARDIAN":
                qs = qs.filter(resident__guardian_id=request.user.id)
            else:
                qs = qs.filter(resident__agent_id=request.user.id)
            # Exclude moved-out residents for Guardian and Agent portals
            qs = qs.filter(resident__group_home_assignments__status="ACTIVE").distinct()

        elif user_role_name == "Nurse":
            # Nurses may ONLY see the nursing transition evaluation form.
            # NOTE: Nurse role has type="STAFF" in the DB — detect by name.
            qs = qs.filter(
                form_code="5_AND_30_DAY_NURSING_TRANSITION_EVALUATION_FORM"
            )

        serializer = ConsentFormSerializer(qs, many=True)

        return Response(
            {
                "status": "success",
                "code": status.HTTP_200_OK,
                "message": "Consent forms fetched successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )
#---------------------------------------------------------------------------------------------------

#---------------------------------------------------------------------------------------------------
class ConsentFormShareAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "consent_forms.share",
    }
    # Sharing is staff-only — portal users never initiate shares

    @extend_schema(
        operation_id="share_consent_form",
        summary="Share consent form via email",
        description="Send a consent form notification email to a guardian or agent.",
        tags=["Consent Forms"],
        request=ConsentFormShareSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request, uuid):
        try:
            serializer = ConsentFormShareSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            recipient_email  = serializer.validated_data.get("recipient_email")
            recipient_emails = serializer.validated_data.get("recipient_emails") or []
            recipient_name   = serializer.validated_data.get("recipient_name")
            media_uuid       = serializer.validated_data.get("media_uuid")
            pdf_base64       = serializer.validated_data.get("pdf_base64")
            pdf_filename     = serializer.validated_data.get("pdf_filename")
            html_content     = serializer.validated_data.get("html_content")
            # "GUARDIAN" or "AGENT" — determines which portal sees this document
            recipient_type   = serializer.validated_data.get("recipient_type")

            # If list not provided, wrap single email for unified processing
            if not recipient_emails and recipient_email:
                recipient_emails = [recipient_email]

            if not recipient_emails:
                return Response(
                    {"status": "error", "message": "recipient_email or recipient_emails is required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            form = ConsentForm.objects.get(
                uuid=uuid,
                deleted_at__isnull=True,
            )

            # Validate a COMPLETED (or SIGNED) entry exists
            latest_entry = (
                form.entries
                .filter(deleted_at__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if not media_uuid and (not latest_entry or latest_entry.status not in ("COMPLETED", "SIGNED")):
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Cannot share form — it must be completed first.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            resident_name = ""
            if form.resident and form.resident.user:
                user = form.resident.user
                first = getattr(user, "first_name", "") or ""
                last = getattr(user, "last_name", "") or ""
                resident_name = f"{first} {last}".strip()

            pdf_bytes = None
            media = None  # guard: may be set below; avoids UnboundLocalError at response build
            if pdf_base64:
                try:
                    from document.services import store_pdf_as_media

                    pdf_bytes = base64.b64decode(pdf_base64, validate=True)
                    filename = os.path.basename(pdf_filename or f"{form.form_name}.pdf")
                    media = store_pdf_as_media(
                        pdf_bytes=pdf_bytes,
                        filename=filename,
                        user=request.user,
                        consent_form=form,
                    )
                    media_uuid = media.id
                except (binascii.Error, ValueError):
                    return Response(
                        {"status": "error", "message": "Invalid PDF data."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                except Exception as pdf_err:
                    logger.error(
                        "PDF storage failed for consent form %s: %s",
                        form.uuid, pdf_err,
                    )
                    # Continue without PDF — emails will still be sent

            elif html_content:
                try:
                    from document.services import generate_pdf_from_html, store_pdf_as_media

                    pdf_bytes = generate_pdf_from_html(html_content)
                    filename = f"{form.form_name}.pdf"
                    media = store_pdf_as_media(
                        pdf_bytes=pdf_bytes,
                        filename=filename,
                        user=request.user,
                        consent_form=form,
                    )
                    media_uuid = media.id
                except Exception as pdf_err:
                    logger.error(
                        "PDF generation failed for consent form %s: %s",
                        form.uuid, pdf_err,
                    )
                    # Continue without PDF — emails will still be sent

            # --- Link existing Media record (uploaded by frontend) ---
            elif media_uuid:
                try:
                    media = Media.objects.get(id=media_uuid, status="active")
                    metadata = media.metadata or {}
                    metadata["consent_form_uuid"] = str(form.uuid)
                    media.metadata = metadata
                    media.save(update_fields=["metadata"])
                except Media.DoesNotExist:
                    logger.warning(
                        "Media %s not found when sharing consent form %s",
                        media_uuid, form.uuid,
                    )

            if media:
                metadata = media.metadata or {}
                media_changed = False
                if recipient_type and recipient_type != "CUSTOM":
                    shared_with = metadata.get("shared_with", [])
                    if isinstance(shared_with, str):
                        shared_with = [shared_with] if shared_with else []
                    if recipient_type not in shared_with:
                        shared_with.append(recipient_type)
                        metadata["shared_with"] = shared_with
                        media_changed = True
                # Stamp the consent entry version on the shared PDF so the
                # guardian/agent portals can label re-shared documents as
                # Version 1 / Version 2.
                if latest_entry and not metadata.get("version"):
                    metadata["version"] = latest_entry.version
                    media_changed = True
                if media_changed:
                    media.metadata = metadata
                    media.save(update_fields=["metadata"])

            # --- Set shared_at and append recipient_type to signer_type ---
            # shared_at is stamped on EVERY share so it always reflects the
            # last share time. The frontend compares it against updated_at to
            # decide whether the form changed since the last share (re-share).
            now = timezone.now()
            update_fields = ["updated_at", "shared_at"]
            form.updated_at = now
            form.shared_at = now
            # Append new recipient_type to signer_type (comma-separated)
            # so both GUARDIAN and AGENT can see this form in their portals.
            if recipient_type and recipient_type != "CUSTOM":
                existing = [t.strip() for t in (form.signer_type or "").split(",") if t.strip()]
                if recipient_type not in existing:
                    existing.append(recipient_type)
                    form.signer_type = ",".join(existing)
                    update_fields.append("signer_type")
            form.save(update_fields=update_fields)

            # Recompute lead status after sharing (may trigger ONBOARDING_IN_PROGRESS)
            recompute_and_save(form.resident, request=request)

            # --- Send emails ---
            shares_succeeded = []
            for email_addr in recipient_emails:
                attachments = None
                is_custom_recipient = (recipient_type == "CUSTOM")
                
                if is_custom_recipient:
                    # For custom emails, we attach the PDF
                    if pdf_bytes:
                        attachments = [{
                            "filename": f"{form.form_name}.pdf",
                            "content": pdf_bytes,
                            "mimetype": "application/pdf"
                        }]
                    elif media_uuid:
                        # If we have an existing media record, get its content
                        target_media = Media.objects.filter(id=media_uuid).first()
                        if target_media:
                            try:
                                attachments = [{
                                    "filename": target_media.original_filename,
                                    "content": target_media.file.read(),
                                    "mimetype": target_media.mime_type or "application/pdf"
                                }]
                            except Exception as e:
                                logger.error("Failed to read media file for attachment: %s", e)

                email_sent = False
                try:
                    result = send_email(
                        to_email=email_addr,
                        subject=f"Consent Form Shared: {form.form_name}",
                        template_name="share_consent_form.html",
                        context={
                            "recipient_name": recipient_name if len(recipient_emails) == 1 and recipient_name else email_addr,
                            "form_name": form.form_name,
                            "resident_name": resident_name or "N/A",
                            "shared_by": f"{request.user.first_name} {request.user.last_name or ''}".strip() or request.user.email,
                            "login_url": f"{settings.FRONTEND_BASE_URL}/login",
                            "is_custom": is_custom_recipient,
                        },
                        attachments=attachments
                    )
                    email_sent = result == 200
                except Exception as email_err:
                    logger.error("Email send failed for %s: %s", email_addr, email_err)
                
                if email_sent:
                    shares_succeeded.append(email_addr)

            # --- Build response ---
            response_data = {}
            if media:
                response_data["media_uuid"] = str(media.id)
            
            if shares_succeeded:
                return Response(
                    {
                        "status": "success",
                        "code": status.HTTP_200_OK,
                        "message": f"Form shared successfully with {', '.join(shares_succeeded)}",
                        "data": response_data,
                    },
                    status=status.HTTP_200_OK,
                )
            
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to send email notifications.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        except ConsentForm.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "Consent form not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except serializers.ValidationError as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Validation failed",
                    "errors": e.detail,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "Failed to share consent form",
                    "details": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
#---------------------------------------------------------------------------------------------------


@api_view(["POST"])
def share_media_only(request, media_uuid):
    """Share one or more uploaded media files (no ConsentForm)."""
    if not request.user.is_authenticated:
        return Response({"status": "error", "message": "Authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
    if not check_perm(request.user, "consent_forms.share"):
        return Response({"status": "error", "message": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)

    requested_media_uuids = request.data.get("media_uuids") or [media_uuid]
    if not isinstance(requested_media_uuids, list):
        return Response(
            {"status": "error", "message": "media_uuids must be a list"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    media_ids = list(dict.fromkeys(str(item) for item in requested_media_uuids if item))
    if str(media_uuid) not in media_ids:
        media_ids.insert(0, str(media_uuid))

    media_by_id = {
        str(item.id): item
        for item in Media.objects.filter(id__in=media_ids, status="active")
    }
    media_items = [media_by_id[item_id] for item_id in media_ids if item_id in media_by_id]
    if not media_items:
        return Response({"status": "error", "message": "Media not found"}, status=status.HTTP_404_NOT_FOUND)

    recipient_email   = (request.data.get("recipient_email")   or "").strip()
    recipient_emails  = request.data.get("recipient_emails")  or []
    recipient_name    = (request.data.get("recipient_name")    or "").strip()
    recipient_type    = (request.data.get("recipient_type")    or "").upper()
    resident_name_req = (request.data.get("resident_name")     or "").strip()

    if not recipient_emails and recipient_email:
        recipient_emails = [recipient_email]

    if not recipient_emails:
        return Response({"status": "error", "message": "recipient_email or recipient_emails is required"}, status=status.HTTP_400_BAD_REQUEST)

    for media in media_items:
        metadata = media.metadata or {}
        # shared_with is a list to support sharing with multiple recipients
        shared_with_list = metadata.get("shared_with", [])
        if isinstance(shared_with_list, str):
            shared_with_list = [shared_with_list] if shared_with_list else []
        if recipient_type and recipient_type != "CUSTOM" and recipient_type not in shared_with_list:
            shared_with_list.append(recipient_type)
        metadata["shared_with"] = shared_with_list
        # Stamped on every share so it always reflects the last share time
        # (used by the staff table to allow re-sharing after changes).
        metadata["shared_at"] = timezone.now().isoformat()
        media.metadata = metadata
        media.save(update_fields=["metadata"])

    shared_by = f"{request.user.first_name} {request.user.last_name or ''}".strip() or request.user.email
    shares_succeeded = []
    is_custom_recipient = (recipient_type == "CUSTOM")
    attachments = None

    if is_custom_recipient:
        attachments = []
        for media in media_items:
            try:
                attachments.append({
                    "filename": media.original_filename,
                    "content": media.file.read(),
                    "mimetype": media.mime_type or "application/pdf",
                })
            except Exception as e:
                logger.error("Failed to read media file %s for attachment: %s", media.id, e)
        attachments = attachments or None

    primary_media = media_items[0]
    form_name = (
        primary_media.original_filename
        if len(media_items) == 1
        else f"{primary_media.original_filename} and {len(media_items) - 1} more file(s)"
    )

    for email_addr in recipient_emails:
        email_sent = False
        try:
            result = send_email(
                to_email=email_addr,
                subject=f"Document Shared: {form_name}",
                template_name="share_consent_form.html",
                context={
                    "recipient_name": recipient_name if len(recipient_emails) == 1 else email_addr,
                    "form_name":      form_name,
                    "resident_name":  resident_name_req,
                    "shared_by":      shared_by,
                    "login_url":      f"{settings.FRONTEND_BASE_URL}/login",
                    "is_custom":      is_custom_recipient,
                },
                attachments=attachments
            )
            email_sent = result == 200
        except Exception as e:
            logger.error("share_media_only email error %s: %s", email_addr, e)
        
        if email_sent:
            shares_succeeded.append(email_addr)

    msg = (
        f"Document shared successfully with {', '.join(shares_succeeded)}"
        if shares_succeeded
        else "Document available in portal (email could not be sent)."
    )
    return Response({"status": "success", "message": msg}, status=status.HTTP_200_OK)
#---------------------------------------------------------------------------------------------------
