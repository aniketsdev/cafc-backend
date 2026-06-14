import logging
import traceback
from rest_framework.views import APIView
from rest_framework.generics import RetrieveAPIView, ListAPIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import HasPermission
from rest_framework.pagination import PageNumberPagination
from django.contrib.contenttypes.models import ContentType
from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError
from django.db import models
from django.utils import timezone

from .models import Media
from .serializers import (
    MediaUploadSerializer,
    MediaSerializer,
    MediaListSerializer,
    MediaUpdateSerializer
)
from .services import MediaService
from .utils import generate_presigned_upload_url, get_content_type_cached
from audit_logs.utils import log_audit

logger = logging.getLogger(__name__)


def _resolve_media_audit_context(content_type, object_id):
    """
    Resolve group home, entity label, composite entity type, and target user
    from media's linked entity. Used to enrich audit log entries with context.

    Returns (group_home, entity_label, entity_type_prefix, resolved_user) where:
      - entity_type_prefix is e.g. "Lead", "Resident", "GroupHome"
      - entity_label is e.g. "for Lead John Doe"
      - resolved_user is the target User instance (or None)
    """
    group_home = None
    entity_label = ""
    entity_type_prefix = ""
    resolved_user = None

    if not content_type or not object_id:
        return group_home, entity_label, entity_type_prefix, None

    try:
        from django.apps import apps

        app_label = content_type.app_label
        model_name = content_type.model

        if app_label == "leads" and model_name == "lead":
            LeadModel = apps.get_model("leads", "Lead")
            lead = LeadModel.objects.select_related(
                "user__role", "user__group_home"
            ).filter(id=object_id).first()
            if lead and lead.user:
                resolved_user = lead.user
                # Categorize based on user role
                role_type = ""
                if hasattr(lead.user, "role") and lead.user.role:
                    role_type = getattr(lead.user.role, "type", "").upper()

                if role_type == "RESIDENT":
                    entity_type_prefix = "Resident"
                    name = f"{lead.user.first_name} {lead.user.last_name}".strip() or "Unknown"
                    entity_label = f"for Resident {name}"
                else:
                    entity_type_prefix = "Lead"
                    name = f"{lead.user.first_name} {lead.user.last_name}".strip() or "Unknown"
                    entity_label = f"for Lead {name}"

                group_home = getattr(lead.user, "group_home", None)
                if not group_home:
                    try:
                        AssignmentModel = apps.get_model("leads", "LeadGroupHomeAssignment")
                        assignment = AssignmentModel.objects.select_related(
                            "group_home"
                        ).filter(lead=lead, status="ACTIVE").first()
                        if assignment:
                            group_home = assignment.group_home
                    except Exception:
                        pass
            else:
                entity_type_prefix = "Lead"

        elif app_label == "incidents" and model_name == "incident":
            entity_type_prefix = "Incident"
            IncidentModel = apps.get_model("incidents", "Incident")
            incident = IncidentModel.objects.select_related(
                "resident__role", "resident__group_home"
            ).filter(id=object_id).first()
            if incident and incident.resident:
                resolved_user = incident.resident
                name = f"{incident.resident.first_name} {incident.resident.last_name}".strip() or "Unknown"
                entity_label = f"for Incident of {name}"
                group_home = getattr(incident.resident, "group_home", None)

        elif app_label == "appointments" and model_name == "appointment":
            entity_type_prefix = "Appointment"
            AppointmentModel = apps.get_model("appointments", "Appointment")
            appt = AppointmentModel.objects.select_related(
                "lead__user__role", "lead__user__group_home"
            ).filter(id=object_id).first()
            if appt and appt.lead and appt.lead.user:
                resolved_user = appt.lead.user
                name = f"{appt.lead.user.first_name} {appt.lead.user.last_name}".strip() or "Unknown"
                entity_label = f"for Appointment of {name}"
                group_home = getattr(appt.lead.user, "group_home", None)

        elif app_label == "group_home" and model_name == "grouphome":
            entity_type_prefix = "GroupHome"
            GroupHomeModel = apps.get_model("group_home", "GroupHome")
            gh = GroupHomeModel.objects.filter(id=object_id).first()
            if gh:
                group_home = gh
                entity_label = f"for Group Home {gh.name}"

        else:
            # Generic fallback: use model name as prefix
            entity_type_prefix = model_name.title()
            ModelClass = content_type.model_class()
            if ModelClass:
                obj = ModelClass.objects.filter(id=object_id).first()
                if obj:
                    entity_label = f"for {model_name.title()} #{object_id}"
                    if hasattr(obj, "group_home"):
                        group_home = obj.group_home
                    elif hasattr(obj, "user") and hasattr(obj.user, "group_home"):
                        group_home = obj.user.group_home
    except Exception:
        pass

    return group_home, entity_label, entity_type_prefix, resolved_user


def _media_type_suffix(alt_text):
    """Return 'Avatar' if media is an avatar (profile picture), else 'Document'."""
    if alt_text and "profile" in (alt_text or "").lower():
        return "Avatar"
    return "Document"


class MediaPagination(PageNumberPagination):
    """Pagination for media list"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class MediaUploadAPIView(APIView):
    """API view for uploading media files.

    DEPRECATED: This endpoint proxies the file through Django to S3, which is
    slow for large files. Use the presigned URL flow instead:
      1. POST /api/media/generate-upload-url/ → get presigned S3 URL
      2. Upload file directly to S3 from the client
      3. POST /api/media/confirm-upload/ → create Media record
    """

    # Same upload capability as the presigned flow — open to any authenticated
    # user (not gated on documents.upload), so a fallback here can't re-introduce
    # the DSP signature-upload 403.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Handle file upload (deprecated — use presigned URL flow)"""
        try:
            serializer = MediaUploadSerializer(
                data=request.data,
                context={'request': request}
            )
            
            if serializer.is_valid():
                media = serializer.save()

                # Audit log: document uploaded
                group_home, entity_label, prefix, target = _resolve_media_audit_context(
                    media.content_type, media.object_id
                )
                suffix = _media_type_suffix(media.alt_text)
                log_audit(
                    request=request,
                    action="CREATE",
                    entity_type=f"{prefix}/{suffix}" if prefix else suffix,
                    entity_id=str(media.id),
                    group_home=group_home,
                    message=f"Uploaded {suffix.lower()} '{media.original_filename}' {entity_label}".strip(),
                    target_user=target,
                )

                response_serializer = MediaSerializer(media)
                response = Response(
                    response_serializer.data,
                    status=status.HTTP_201_CREATED
                )
                response['Deprecation'] = 'true'
                response['Sunset'] = 'Use POST /api/media/generate-upload-url/ + POST /api/media/confirm-upload/ instead'
                return response

            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        except ValidationError as e:
            # Handle validation errors from serializer
            logger.error(f"Validation error during upload: {str(e)}")
            error_message = str(e)
            if hasattr(e, 'detail'):
                if isinstance(e.detail, dict):
                    error_message = '; '.join([f"{k}: {v[0] if isinstance(v, list) else v}" for k, v in e.detail.items()])
                elif isinstance(e.detail, list):
                    error_message = '; '.join([str(item) for item in e.detail])
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            
            # Comprehensive error logging
            logger.error(
                "Media upload failed: S3 error",
                extra={
                    'error_type': 'S3UploadError',
                    'error_code': error_code,
                    'error_message': error_msg,
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'user_email': request.user.email if request.user.is_authenticated else None,
                    'operation': 'media_upload',
                    'stack_trace': traceback.format_exc()
                }
            )
            
            # Provide more specific error messages based on error code
            if error_code == 'NoSuchBucket':
                user_message = 'Storage bucket not found. Please contact administrator.'
            elif error_code == 'AccessDenied':
                user_message = 'Access denied to storage. Please contact administrator.'
            elif error_code == 'InvalidAccessKeyId':
                user_message = 'Storage configuration error. Please contact administrator.'
            elif error_code == 'SignatureDoesNotMatch':
                user_message = 'Storage authentication failed. Please contact administrator.'
            else:
                user_message = f'Failed to upload file to storage: {error_msg}'
            
            return Response(
                {'error': user_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except UnidentifiedImageError as e:
            logger.error(f"Image processing error: {str(e)}")
            return Response(
                {'error': 'Invalid image file. Please check the file format.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            error_traceback = traceback.format_exc()
            
            # Comprehensive error logging
            logger.error(
                "Media upload failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'user_email': request.user.email if request.user.is_authenticated else None,
                    'operation': 'media_upload',
                    'stack_trace': error_traceback
                }
            )
            
            # In DEBUG mode, include the actual error message
            from django.conf import settings
            error_message = 'An error occurred while uploading the file. Please try again.'
            if settings.DEBUG:
                error_message = f'An error occurred while uploading the file: {str(e)}'
            
            return Response(
                {'error': error_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MediaRetrieveAPIView(RetrieveAPIView):
    """API view for retrieving a single media file"""

    serializer_class = MediaSerializer
    queryset = Media.objects.filter(status='active').select_related(
        'uploaded_by', 'content_type'
    )
    lookup_field = 'pk'
    permission_classes = [IsAuthenticated]
    # No HasPermission gate: retrieve is used internally (presigned URLs, form previews).
    # G/A scope filtering is enforced inside MediaListAPIView.get_queryset().
    
    def retrieve(self, request, *args, **kwargs):
        """Retrieve media by ID"""
        try:
            instance = self.get_object()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except Media.DoesNotExist:
            return Response(
                {'error': 'Media not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Error retrieving media: {str(e)}\n{error_traceback}")
            
            # In DEBUG mode, include the actual error message
            from django.conf import settings
            error_message = 'An error occurred while retrieving the media.'
            if settings.DEBUG:
                error_message = f'An error occurred while retrieving the media: {str(e)}'
            
            return Response(
                {'error': error_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MediaDeleteAPIView(APIView):
    """API view for deleting media files"""
    
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "DELETE": "documents.delete",
    }
    
    def delete(self, request, pk):
        """Delete media by ID"""
        try:
            # Get media instance
            try:
                media = Media.objects.select_related(
                    'uploaded_by', 'content_type'
                ).get(id=pk, status='active')
            except Media.DoesNotExist:
                return Response(
                    {'error': 'Media not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check permissions: user must be uploader or admin
            # Check if user is superuser or has admin/staff role
            is_superuser = getattr(request.user, 'is_superuser', False)
            is_admin_role = False
            if hasattr(request.user, 'role') and request.user.role:
                role_type = getattr(request.user.role, 'type', '').upper()
                is_admin_role = role_type in ['ADMIN', 'STAFF']
            
            is_admin = is_superuser or is_admin_role
            is_uploader = media.uploaded_by == request.user
            
            if not (is_admin or is_uploader):
                return Response(
                    {'error': 'You do not have permission to delete this media.'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Capture audit context before deletion
            group_home, entity_label, prefix, target = _resolve_media_audit_context(
                media.content_type, media.object_id
            )
            media_id_str = str(media.id)
            media_filename = media.original_filename
            suffix = _media_type_suffix(media.alt_text)
            doc_entity_type = f"{prefix}/{suffix}" if prefix else suffix

            # Get hard_delete parameter from query string
            hard_delete = request.query_params.get('hard_delete', 'false').lower() == 'true'

            # Call MediaService to delete file
            success, error = MediaService.delete_file(media.id, hard_delete=hard_delete)

            if not success:
                return Response(
                    {'error': error or 'Failed to delete media'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Audit log: document deleted
            log_audit(
                request=request,
                action="DELETE",
                entity_type=doc_entity_type,
                entity_id=media_id_str,
                group_home=group_home,
                message=f"Deleted {suffix.lower()} '{media_filename}' {entity_label}".strip(),
                target_user=target,
            )

            return Response(
                status=status.HTTP_204_NO_CONTENT
            )
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            
            logger.error(
                "Media deletion failed: S3 error",
                extra={
                    'error_type': 'S3DeletionError',
                    'error_code': error_code,
                    'error_message': error_msg,
                    'media_id': str(pk),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'operation': 'media_delete',
                    'stack_trace': traceback.format_exc()
                }
            )
            
            # Provide more specific error messages based on error code
            if error_code == 'NoSuchBucket':
                user_message = 'Storage bucket not found. Please contact administrator.'
            elif error_code == 'AccessDenied':
                user_message = 'Access denied to storage. Please contact administrator.'
            elif error_code == 'NoSuchKey':
                user_message = 'File not found in storage. It may have already been deleted.'
            else:
                user_message = f'Failed to delete file from storage: {error_msg}'
            
            return Response(
                {'error': user_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            error_traceback = traceback.format_exc()
            
            logger.error(
                "Media deletion failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    'media_id': str(pk),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'operation': 'media_delete',
                    'stack_trace': error_traceback
                }
            )
            
            # In DEBUG mode, include the actual error message
            from django.conf import settings
            error_message = 'An error occurred while deleting the media. Please try again.'
            if settings.DEBUG:
                error_message = f'An error occurred while deleting the media: {str(e)}'
            
            return Response(
                {'error': error_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MediaUpdateAPIView(APIView):
    """API view for updating existing media files"""
    
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PUT": "documents.edit",
    }
    
    def put(self, request, pk):
        """Update media by ID - replaces existing file with new one"""
        try:
            # Get media instance
            try:
                media = Media.objects.select_related(
                    'uploaded_by', 'content_type'
                ).get(id=pk, status='active')
            except Media.DoesNotExist:
                return Response(
                    {'error': 'Media not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check permissions: user must be uploader or admin
            # Check if user is superuser or has admin/staff role
            is_superuser = getattr(request.user, 'is_superuser', False)
            is_admin_role = False
            if hasattr(request.user, 'role') and request.user.role:
                role_type = getattr(request.user.role, 'type', '').upper()
                is_admin_role = role_type in ['ADMIN', 'STAFF']
            
            is_admin = is_superuser or is_admin_role
            is_uploader = media.uploaded_by == request.user
            
            if not (is_admin or is_uploader):
                return Response(
                    {'error': 'You do not have permission to update this media.'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Validate and update
            serializer = MediaUpdateSerializer(
                media,
                data=request.data,
                context={'request': request},
                partial=True
            )
            
            if serializer.is_valid():
                updated_media = serializer.save()

                # Audit log: document updated
                group_home, entity_label, prefix, target = _resolve_media_audit_context(
                    updated_media.content_type, updated_media.object_id
                )
                suffix = _media_type_suffix(updated_media.alt_text)
                log_audit(
                    request=request,
                    action="UPDATE",
                    entity_type=f"{prefix}/{suffix}" if prefix else suffix,
                    entity_id=str(updated_media.id),
                    group_home=group_home,
                    message=f"Updated {suffix.lower()} '{updated_media.original_filename}' {entity_label}".strip(),
                    target_user=target,
                )

                response_serializer = MediaSerializer(updated_media)
                return Response(
                    response_serializer.data,
                    status=status.HTTP_200_OK
                )

            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        except ValidationError as e:
            # Handle validation errors from serializer
            logger.error(f"Validation error during update: {str(e)}")
            error_message = str(e)
            if hasattr(e, 'detail'):
                if isinstance(e.detail, dict):
                    error_message = '; '.join([f"{k}: {v[0] if isinstance(v, list) else v}" for k, v in e.detail.items()])
                elif isinstance(e.detail, list):
                    error_message = '; '.join([str(item) for item in e.detail])
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            
            logger.error(
                "Media update failed: S3 error",
                extra={
                    'error_type': 'S3UpdateError',
                    'error_code': error_code,
                    'error_message': error_msg,
                    'media_id': str(pk),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'operation': 'media_update',
                    'stack_trace': traceback.format_exc()
                }
            )
            
            # Provide more specific error messages based on error code
            if error_code == 'NoSuchBucket':
                user_message = 'Storage bucket not found. Please contact administrator.'
            elif error_code == 'AccessDenied':
                user_message = 'Access denied to storage. Please contact administrator.'
            elif error_code == 'InvalidAccessKeyId':
                user_message = 'Storage configuration error. Please contact administrator.'
            elif error_code == 'SignatureDoesNotMatch':
                user_message = 'Storage authentication failed. Please contact administrator.'
            else:
                user_message = f'Failed to update file to storage: {error_msg}'
            
            return Response(
                {'error': user_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except UnidentifiedImageError as e:
            logger.error(f"Image processing error: {str(e)}")
            return Response(
                {'error': 'Invalid image file. Please check the file format.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            error_traceback = traceback.format_exc()
            
            logger.error(
                "Media update failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    'media_id': str(pk),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'operation': 'media_update',
                    'stack_trace': error_traceback
                }
            )
            
            # In DEBUG mode, include the actual error message
            from django.conf import settings
            error_message = 'An error occurred while updating the media. Please try again.'
            if settings.DEBUG:
                error_message = f'An error occurred while updating the media: {str(e)}'
            
            return Response(
                {'error': error_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MediaListAPIView(ListAPIView):
    """API view for listing media files with filters"""
    
    serializer_class = MediaListSerializer
    pagination_class = MediaPagination
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "documents.view",  # Nurse does NOT have this → 403
    }
    allow_portal_roles = {"GET"}  # G/A allowed through; inline filter limits their data
    
    def get_queryset(self):
        queryset = Media.objects.filter(status='active').select_related(
            'uploaded_by', 'content_type'
        )

        content_type_str = self.request.query_params.get('content_type')
        object_uuid = self.request.query_params.get('object_uuid')

        if content_type_str and object_uuid:
            try:
                app_label, model = content_type_str.split('.')

                content_type = get_content_type_cached(app_label, model)

                # Resolve UUID to internal object_id
                model_class = content_type.model_class()
                if model_class:
                    obj = model_class.objects.filter(uuid=object_uuid).first()
                    if not obj:
                        return Media.objects.none()
                    queryset = queryset.filter(
                        content_type=content_type,
                        object_id=obj.id
                    )
                else:
                    return Media.objects.none()
            except (ValueError, ContentType.DoesNotExist):
                return Media.objects.none()
        
        # ----------------------------------
        # SIGNED FILTER (NEW)
        # ----------------------------------
        signed = self.request.query_params.get("signed")
        user = self.request.user
        user_role_type = ""
        if hasattr(user, "role") and user.role:
            user_role_type = getattr(user.role, "type", "").upper()

        if signed is not None:
            role_signature_key = f"metadata__role_signatures__{user_role_type}"
            role_signed_key = f"{role_signature_key}__signed"
            if user_role_type in ("GUARDIAN", "AGENT"):
                if signed.lower() == "true":
                    queryset = queryset.filter(
                        models.Q(**{role_signed_key: True}) |
                        models.Q(**{f"{role_signature_key}__isnull": True}, metadata__signed=True)
                    )
                elif signed.lower() == "false":
                    queryset = queryset.filter(
                        models.Q(**{role_signed_key: False}) |
                        (
                            models.Q(**{f"{role_signature_key}__isnull": True}) &
                            (
                                models.Q(metadata__signed=False) |
                                models.Q(metadata__signed__isnull=True)
                            )
                        )
                    )
            elif signed.lower() == "true":
                queryset = queryset.filter(metadata__signed=True)
            elif signed.lower() == "false":
                queryset = queryset.filter(
                    models.Q(metadata__signed=False) |
                    models.Q(metadata__signed__isnull=True)
                )

        # ----------------------------------
        # RBAC: Role-Based Document Filtering
        # ----------------------------------
        # Guardian and Agent portals ONLY see documents explicitly shared
        # with their role via the Share button in Consent & Forms.
        #
        # A shared document must have:
        #   - metadata.consent_form_uuid set (it is a consent-form PDF)
        #   - ConsentForm.signer_type == user's role (GUARDIAN or AGENT)
        #   - ConsentForm.shared_at is not null (was actually shared)
        #
        # Plain uploaded documents (no consent_form_uuid) are NEVER shown
        # in portals — they are for admin/staff use only.
        if user_role_type in ("GUARDIAN", "AGENT"):
            from document.models import ConsentForm

            # Collect UUIDs of consent forms explicitly shared with THIS role
            allowed_cf_uuids = list(
                str(u) for u in
                ConsentForm.objects.filter(
                    deleted_at__isnull=True,
                    shared_at__isnull=False,
                ).filter(
                    # signer_type may be "GUARDIAN", "AGENT", or "GUARDIAN,AGENT"
                    models.Q(signer_type__icontains=user_role_type)
                ).values_list("uuid", flat=True)
            )

            if allowed_cf_uuids:
                queryset = queryset.filter(
                    models.Q(metadata__shared_with__contains=[user_role_type]) |
                    models.Q(
                        metadata__shared_with__isnull=True,
                        metadata__consent_form_uuid__in=allowed_cf_uuids,
                    )
                )
            else:
                # Show only documents directly shared with this role
                queryset = queryset.filter(metadata__shared_with__contains=[user_role_type])


        # Optional filters
        file_type = self.request.query_params.get('file_type')
        if file_type:
            queryset = queryset.filter(file_type=file_type)

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(original_filename__icontains=search)

        # Date filter: filter by updated_at date (matches "Last Updated" in UI)
        date_param = self.request.query_params.get('date')
        if date_param:
            try:
                from datetime import datetime, timedelta
                from django.utils.dateparse import parse_datetime
                from django.utils.timezone import is_aware
                
                # Check if it's a full ISO datetime with timezone info (Option 2: strict local bounds)
                dt = parse_datetime(date_param)
                if dt and is_aware(dt):
                    end_dt = dt + timedelta(days=1)
                    queryset = queryset.filter(updated_at__gte=dt, updated_at__lt=end_dt)
                else:
                    # Fallback to simple date parsing using UTC assumptions
                    date_value = datetime.strptime(date_param, '%Y-%m-%d').date()
                    queryset = queryset.filter(updated_at__date=date_value)
            except ValueError:
                pass

        ordering = self.request.query_params.get('ordering', '-uploaded_at')
        queryset = queryset.order_by(ordering)

        return queryset

    
    def list(self, request, *args, **kwargs):
        """List media with error handling"""
        # ---------------------------------------------------------------
        # Nurse hard-deny: Nurses must NOT access the general document
        # library. Their 5 & 30 Day form PDFs are surfaced separately
        # through ConsentFormListAPIView with an inline form_code filter.
        # This check fires even if the nurse has documents.view in the DB.
        # ---------------------------------------------------------------
        user_role_name = getattr(getattr(request.user, "role", None), "name", "")
        if user_role_name == "Nurse":
            return Response(
                {"detail": "You do not have permission to perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            return super().list(request, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error listing media: {str(e)}")
            return Response(
                {'error': 'An error occurred while retrieving the media list.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class MediaSignAPIView(APIView):
    """API view for signing a media document"""

    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "consent_forms.sign",  # Staff: Coordinator has this
    }
    allow_portal_roles = {"POST"}  # Guardians/Agents sign their own documents

    def post(self, request, pk):
        """Sign a document by updating its metadata"""
        try:
            try:
                media = Media.objects.select_related(
                    'uploaded_by', 'content_type'
                ).get(id=pk, status='active')
            except Media.DoesNotExist:
                return Response(
                    {'error': 'Media not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            user_role_type = getattr(
                getattr(request.user, "role", None),
                "type",
                "",
            ).upper()
            shared_with = (media.metadata or {}).get("shared_with", [])
            if isinstance(shared_with, str):
                shared_with = [shared_with] if shared_with else []
            if (
                user_role_type in ("GUARDIAN", "AGENT")
                and shared_with
                and user_role_type not in shared_with
            ):
                return Response(
                    {'error': 'You are not authorized to sign this document.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            metadata = media.metadata or {}
            role_signatures = metadata.get("role_signatures") or {}
            is_role_specific_upload = (
                user_role_type in ("GUARDIAN", "AGENT")
                and not metadata.get("consent_form_uuid")
            )
            is_already_signed = (
                role_signatures.get(user_role_type, {}).get("signed", False)
                if is_role_specific_upload
                else media.is_signed
            )

            # Check if already signed
            if is_already_signed:
                return Response(
                    {'error': 'Document is already signed.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Accept optional signature data and signer name from request body
            signature_data = request.data.get("signature_data")
            signer_name = request.data.get("signer_name")

            if is_role_specific_upload:
                now = timezone.now()
                role_signatures[user_role_type] = {
                    "signed": True,
                    "signed_by": str(request.user.id),
                    "signed_date": now.isoformat(),
                    **({"signature_data": signature_data} if signature_data else {}),
                    **({"signer_name": signer_name} if signer_name else {}),
                }
                media.metadata = {
                    **metadata,
                    "role_signatures": role_signatures,
                }
                media.updated_at = now
                media.save(update_fields=["metadata", "updated_at"])
            else:
                # Consent-form PDFs have separate media copies per role.
                media.mark_signed(
                    request.user,
                    signature_data=signature_data,
                    signer_name=signer_name,
                )

            # Audit log
            group_home, entity_label, prefix, target = _resolve_media_audit_context(
                media.content_type, media.object_id
            )
            suffix = _media_type_suffix(media.alt_text)
            log_audit(
                request=request,
                action="UPDATE",
                entity_type=f"{prefix}/{suffix}" if prefix else suffix,
                entity_id=str(media.id),
                group_home=group_home,
                message=f"Signed {suffix.lower()} '{media.original_filename}' {entity_label}".strip(),
                target_user=target,
            )

            serializer = MediaSerializer(media, context={"request": request})
            return Response(
                serializer.data,
                status=status.HTTP_200_OK
            )

        except Exception as e:
            error_traceback = traceback.format_exc()
            logger.error(
                "Media signing failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    'media_id': str(pk),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'operation': 'media_sign',
                    'stack_trace': error_traceback
                }
            )
            from django.conf import settings
            error_message = 'An error occurred while signing the document.'
            if settings.DEBUG:
                error_message = f'An error occurred while signing the document: {str(e)}'
            return Response(
                {'error': error_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class GeneratePresignedUploadURLAPIView(APIView):
    """API view for generating presigned S3 URLs for direct client uploads"""

    # Generic upload infrastructure used across incidents/profile/etc. Gating it
    # behind the documents-module `documents.upload` permission blocked DSPs (and
    # other staff) from attaching incident signatures. Any authenticated user may
    # request a presigned URL; the caller flow controls what gets uploaded.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Generate presigned S3 URL for file upload"""
        try:
            # Get request data
            file_name = request.data.get('file_name')
            file_type = request.data.get('file_type')
            file_size = request.data.get('file_size')
            content_type_app = request.data.get('content_type_app')
            content_type_model = request.data.get('content_type_model')
            object_uuid = request.data.get('object_uuid')

            # Validate required fields
            if not all([file_name, file_type, file_size]):
                return Response(
                    {
                        'error': 'Missing required fields: file_name, file_type, and file_size are required'
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate file_size is integer
            try:
                file_size = int(file_size)
            except (ValueError, TypeError):
                logger.error(
                    "Presigned URL generation failed: Invalid file size",
                    extra={
                        'error_type': 'ValidationError',
                        'file_name': file_name,
                        'file_type': file_type,
                        'file_size': request.data.get('file_size'),
                        'user_id': request.user.id if request.user.is_authenticated else None,
                        'operation': 'generate_presigned_upload_url',
                        'stack_trace': traceback.format_exc()
                    }
                )
                return Response(
                    {'error': 'file_size must be a valid integer'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Resolve object_uuid to object_id
            object_id = None
            if object_uuid and content_type_app and content_type_model:
                try:
                    ct = get_content_type_cached(content_type_app, content_type_model)
                    model_class = ct.model_class()
                    if model_class:
                        obj = model_class.objects.filter(uuid=object_uuid).first()
                        if obj:
                            object_id = obj.id
                except ContentType.DoesNotExist:
                    pass

            # Generate presigned URL
            presigned_data, error = generate_presigned_upload_url(
                file_name=file_name,
                file_type=file_type,
                file_size=file_size,
                user_id=str(request.user.id) if request.user.is_authenticated else None,
                content_type_app=content_type_app,
                content_type_model=content_type_model,
                object_id=object_id
            )
            
            if error:
                return Response(
                    {'error': error},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            return Response(
                presigned_data,
                status=status.HTTP_200_OK
            )
            
        except Exception as e:
            logger.error(
                "Presigned URL generation failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'request_data': request.data,
                    'operation': 'generate_presigned_upload_url',
                    'stack_trace': traceback.format_exc()
                }
            )
            return Response(
                {'error': 'An error occurred while generating upload URL'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ConfirmUploadAPIView(APIView):
    """API view for confirming S3 upload and creating Media record"""

    # Pairs with GeneratePresignedUploadURLAPIView — see note there. Any
    # authenticated user may confirm an upload and create the Media record.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Confirm file uploaded to S3 and create Media record"""
        try:
            # Get request data
            s3_key = request.data.get('s3_key')
            original_filename = request.data.get('original_filename')
            file_type = request.data.get('file_type')
            file_size = request.data.get('file_size')
            content_type_app = request.data.get('content_type_app')
            content_type_model = request.data.get('content_type_model')
            object_uuid = request.data.get('object_uuid')
            alt_text = request.data.get('alt_text')
            description = request.data.get('description')

            # Validate required fields
            if not all([s3_key, original_filename, file_type, file_size]):
                logger.error(
                    "Upload confirmation failed: Missing required fields",
                    extra={
                        'error_type': 'ValidationError',
                        'missing_fields': [k for k, v in {
                            's3_key': s3_key,
                            'original_filename': original_filename,
                            'file_type': file_type,
                            'file_size': file_size
                        }.items() if not v],
                        'user_id': request.user.id if request.user.is_authenticated else None,
                        'operation': 'confirm_upload',
                        'stack_trace': traceback.format_exc()
                    }
                )
                return Response(
                    {
                        'error': 'Missing required fields: s3_key, original_filename, file_type, and file_size are required'
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate file_size is integer
            try:
                file_size = int(file_size)
            except (ValueError, TypeError):
                logger.error(
                    "Upload confirmation failed: Invalid file size",
                    extra={
                        'error_type': 'ValidationError',
                        's3_key': s3_key,
                        'file_name': original_filename,
                        'file_size': request.data.get('file_size'),
                        'user_id': request.user.id if request.user.is_authenticated else None,
                        'operation': 'confirm_upload',
                        'stack_trace': traceback.format_exc()
                    }
                )
                return Response(
                    {'error': 'file_size must be a valid integer'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Resolve ContentType if provided
            content_type = None
            if content_type_app and content_type_model:
                try:
                    content_type = get_content_type_cached(content_type_app, content_type_model)
                except ContentType.DoesNotExist:
                    logger.error(
                        "Upload confirmation failed: ContentType not found",
                        extra={
                            'error_type': 'ContentTypeError',
                            'content_type_app': content_type_app,
                            'content_type_model': content_type_model,
                            's3_key': s3_key,
                            'user_id': request.user.id if request.user.is_authenticated else None,
                            'operation': 'confirm_upload',
                            'stack_trace': traceback.format_exc()
                        }
                    )
                    return Response(
                        {'error': f"ContentType not found for {content_type_app}.{content_type_model}"},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Resolve object_uuid to object_id
            object_id = None
            if content_type and object_uuid:
                model_class = content_type.model_class()
                if model_class:
                    obj = model_class.objects.filter(uuid=object_uuid).first()
                    if obj:
                        object_id = obj.id

            # Confirm upload and create Media record
            media, error = MediaService.confirm_s3_upload(
                s3_key=s3_key,
                original_filename=original_filename,
                file_type=file_type,
                file_size=file_size,
                user=request.user,
                content_type=content_type,
                object_id=object_id,
                alt_text=alt_text,
                description=description
            )
            
            if error:
                return Response(
                    {'error': error},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Audit log: document uploaded via presigned URL
            # Skip audit log for group home avatar uploads — the group home
            # create/update log already covers it and logging it separately
            # creates duplicate noise in the audit trail.
            group_home, entity_label, prefix, target = _resolve_media_audit_context(
                content_type, object_id
            )
            suffix = _media_type_suffix(alt_text)
            is_group_home_avatar = (
                prefix == "GroupHome" and suffix == "Avatar"
            )
            if not is_group_home_avatar:
                log_audit(
                    request=request,
                    action="CREATE",
                    entity_type=f"{prefix}/{suffix}" if prefix else suffix,
                    entity_id=str(media.id),
                    group_home=group_home,
                    message=f"Uploaded {suffix.lower()} '{original_filename}' {entity_label}".strip(),
                    target_user=target,
                )

            # Serialize and return Media instance
            serializer = MediaSerializer(media)
            return Response(
                serializer.data,
                status=status.HTTP_201_CREATED
            )

        except Exception as e:
            logger.error(
                "Upload confirmation failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    'user_id': request.user.id if request.user.is_authenticated else None,
                    'request_data': request.data,
                    'operation': 'confirm_upload',
                    'stack_trace': traceback.format_exc()
                }
            )
            return Response(
                {'error': 'An error occurred while confirming upload'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
