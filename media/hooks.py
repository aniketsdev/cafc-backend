"""
Media Hooks - Simple utility functions for media operations
Use these hooks in your views to easily handle media upload, update, and delete operations.

Note: For better performance, consider using the presigned URL flow:
    POST /api/media/generate-upload-url/ - Get presigned URL
    Upload file directly to S3
    POST /api/media/confirm-upload/ - Confirm and create Media record

Example usage in a profile view:
    from media.hooks import upload_media, update_media, delete_media
    
    # Upload new avatar (direct upload - slower, but simpler)
    media_id, error = upload_media(
        file=request.FILES.get('avatar'),
        user=request.user,
        content_type_app='accounts',
        content_type_model='User',
        object_id=user.id,
        alt_text='Profile picture'
    )
    
    # Update existing avatar
    media, error = update_media(
        media_id=existing_media_id,
        file=request.FILES.get('avatar'),
        user=request.user
    )
    
    # Delete avatar
    success, error = delete_media(
        media_id=media_id,
        user=request.user
    )
"""
import logging
import traceback
from typing import Tuple, Optional
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import UploadedFile

from .models import Media
from .services import MediaService
from .utils import get_content_type_cached

logger = logging.getLogger(__name__)


def upload_media(
    file: UploadedFile,
    user,
    content_type_app: Optional[str] = None,
    content_type_model: Optional[str] = None,
    object_id: Optional[int] = None,
    alt_text: Optional[str] = None,
    description: Optional[str] = None
) -> Tuple[Optional[Media], Optional[str]]:
    """
    Hook to upload a new media file.
    
    Args:
        file: The uploaded file object
        user: The user uploading the file
        content_type_app: Optional app name (e.g., 'accounts')
        content_type_model: Optional model name (e.g., 'User')
        object_id: Optional object ID for the related object
        alt_text: Optional alt text for the file
        description: Optional description for the file
    
    Returns:
        Tuple[Optional[Media], Optional[str]]: (Media instance, None) on success or (None, error message) on failure
    
    Example:
        media, error = upload_media(
            file=request.FILES.get('avatar'),
            user=request.user,
            content_type_app='accounts',
            content_type_model='User',
            object_id=user.id,
            alt_text='Profile picture'
        )
        if error:
            return Response({'error': error}, status=400)
        # Use media.id to save to your model
    """
    try:
        # Resolve ContentType if provided
        content_type = None
        if content_type_app and content_type_model:
            try:
                content_type = get_content_type_cached(content_type_app, content_type_model)
            except ContentType.DoesNotExist:
                return None, f"ContentType not found for {content_type_app}.{content_type_model}"
        
        # Call MediaService to upload file
        media, error = MediaService.upload_file(
            file=file,
            user=user,
            content_type=content_type,
            object_id=object_id,
            alt_text=alt_text,
            description=description
        )
        
        if error:
            logger.error(
                "Media upload failed via hook",
                extra={
                    'error_type': 'MediaUploadError',
                    'error_message': error,
                    'user_id': user.id if user else None,
                    'user_email': user.email if user else None,
                    'file_name': file.name if file else None,
                    'file_size': file.size if file else None,
                    'content_type_app': content_type_app,
                    'content_type_model': content_type_model,
                    'object_id': object_id,
                    'operation': 'upload_media_hook',
                    'stack_trace': traceback.format_exc()
                }
            )
            return None, error
        
        return media, None
        
    except Exception as e:
        error_msg = f"Unexpected error uploading media: {str(e)}"
        logger.error(
            "Media upload failed via hook: Unexpected error",
            extra={
                'error_type': 'UnexpectedError',
                'error_message': str(e),
                'user_id': user.id if user else None,
                'user_email': user.email if user else None,
                'file_name': file.name if file else None,
                'operation': 'upload_media_hook',
                'stack_trace': traceback.format_exc()
            }
        )
        return None, error_msg


def update_media(
    media_id: str,
    file: UploadedFile,
    user,
    alt_text: Optional[str] = None,
    description: Optional[str] = None
) -> Tuple[Optional[Media], Optional[str]]:
    """
    Hook to update an existing media file (replaces the file but keeps the same ID).
    
    Args:
        media_id: UUID of the existing media record
        file: The new file to replace the existing one
        user: The user making the update
        alt_text: Optional alt text to update
        description: Optional description to update
    
    Returns:
        Tuple[Optional[Media], Optional[str]]: (Media instance, None) on success or (None, error message) on failure
    
    Example:
        media, error = update_media(
            media_id=user.profile_picture_id,
            file=request.FILES.get('avatar'),
            user=request.user
        )
        if error:
            return Response({'error': error}, status=400)
        # Media ID remains the same, so no need to update your model
    """
    try:
        # Check if media exists and user has permission
        try:
            media = Media.objects.get(id=media_id, status='active')
        except Media.DoesNotExist:
            return None, "Media not found"
        
        # Check permissions: user must be uploader or admin
        is_superuser = getattr(user, 'is_superuser', False)
        is_admin_role = False
        if hasattr(user, 'role') and user.role:
            role_type = getattr(user.role, 'type', '').upper()
            is_admin_role = role_type in ['ADMIN', 'STAFF']
        
        is_admin = is_superuser or is_admin_role
        is_uploader = media.uploaded_by == user
        
        if not (is_admin or is_uploader):
            return None, "You do not have permission to update this media"
        
        # Call MediaService to update file
        updated_media, error = MediaService.update_file(
            media_id=media_id,
            file=file,
            user=user,
            alt_text=alt_text,
            description=description
        )
        
        if error:
            logger.error(
                "Media update failed via hook",
                extra={
                    'error_type': 'MediaUpdateError',
                    'error_message': error,
                    'media_id': media_id,
                    'user_id': user.id if user else None,
                    'user_email': user.email if user else None,
                    'file_name': file.name if file else None,
                    'operation': 'update_media_hook',
                    'stack_trace': traceback.format_exc()
                }
            )
            return None, error
        
        return updated_media, None
        
    except Exception as e:
        error_msg = f"Unexpected error updating media: {str(e)}"
        logger.error(
            "Media update failed via hook: Unexpected error",
            extra={
                'error_type': 'UnexpectedError',
                'error_message': str(e),
                'media_id': media_id,
                'user_id': user.id if user else None,
                'operation': 'update_media_hook',
                'stack_trace': traceback.format_exc()
            }
        )
        return None, error_msg


def delete_media(
    media_id: str,
    user,
    hard_delete: bool = False
) -> Tuple[bool, Optional[str]]:
    """
    Hook to delete a media file.
    
    Args:
        media_id: UUID of the media record to delete
        user: The user making the delete request
        hard_delete: If True, permanently delete from S3 and database. If False, soft delete (default)
    
    Returns:
        Tuple[bool, Optional[str]]: (True, None) on success or (False, error message) on failure
    
    Example:
        success, error = delete_media(
            media_id=user.profile_picture_id,
            user=request.user,
            hard_delete=False  # Soft delete by default
        )
        if error:
            return Response({'error': error}, status=400)
        # Set user.profile_picture_id = None after successful delete
    """
    try:
        # Check if media exists
        try:
            media = Media.objects.get(id=media_id)
        except Media.DoesNotExist:
            return False, "Media not found"
        
        # Check permissions: user must be uploader or admin
        is_superuser = getattr(user, 'is_superuser', False)
        is_admin_role = False
        if hasattr(user, 'role') and user.role:
            role_type = getattr(user.role, 'type', '').upper()
            is_admin_role = role_type in ['ADMIN', 'STAFF']
        
        is_admin = is_superuser or is_admin_role
        is_uploader = media.uploaded_by == user
        
        if not (is_admin or is_uploader):
            return False, "You do not have permission to delete this media"
        
        # Call MediaService to delete file
        success, error = MediaService.delete_file(
            media_id=media_id,
            hard_delete=hard_delete
        )
        
        if error:
            logger.error(
                "Media deletion failed via hook",
                extra={
                    'error_type': 'MediaDeletionError',
                    'error_message': error,
                    'media_id': media_id,
                    'user_id': user.id if user else None,
                    'user_email': user.email if user else None,
                    'hard_delete': hard_delete,
                    'operation': 'delete_media_hook',
                    'stack_trace': traceback.format_exc()
                }
            )
            return False, error
        
        return True, None
        
    except Exception as e:
        error_msg = f"Unexpected error deleting media: {str(e)}"
        logger.error(
            "Media deletion failed via hook: Unexpected error",
            extra={
                'error_type': 'UnexpectedError',
                'error_message': str(e),
                'media_id': media_id,
                'user_id': user.id if user else None,
                'operation': 'delete_media_hook',
                'stack_trace': traceback.format_exc()
            }
        )
        return False, error_msg


def get_media_url(media_id: str) -> Optional[str]:
    """
    Hook to get the signed URL for a media file.
    
    Args:
        media_id: UUID of the media record
    
    Returns:
        Optional[str]: Signed URL or None if media not found
    
    Example:
        url = get_media_url(user.profile_picture_id)
        if url:
            # Use url in your response
    """
    try:
        media = Media.objects.get(id=media_id, status='active')
        return media.get_file_url()
    except Media.DoesNotExist:
        return None
    except Exception as e:
        logger.error(f"Error getting media URL: {str(e)}")
        return None
