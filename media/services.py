import logging
import traceback
import io
from typing import Tuple, Optional
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.core.exceptions import ValidationError
from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError
import boto3
from django.conf import settings

from .models import Media
from .utils import (
    validate_file_size,
    validate_file_type,
    get_file_category,
    get_image_dimensions,
    get_content_type_cached,
)

logger = logging.getLogger(__name__)

from .s3_client import get_s3_client


class MediaService:
    """Service layer for media operations"""
    
    @staticmethod
    def upload_file(
        file,
        user,
        content_type=None,
        object_id=None,
        alt_text=None,
        description=None
    ) -> Tuple[Optional[Media], Optional[str]]:
        """
        Upload a file and create Media instance.
        
        Args:
            file: Uploaded file object
            user: User instance who uploaded the file
            content_type: Optional ContentType instance
            object_id: Optional object ID for generic relationship
            alt_text: Optional alt text for the file
            description: Optional description for the file
        
        Returns:
            Tuple[Optional[Media], Optional[str]]: (Media instance, None) on success or (None, error message) on failure
        """
        try:
            # Validate file size
            is_valid_size, size_error = validate_file_size(file)
            if not is_valid_size:
                return None, size_error
            
            # Validate file type
            is_valid_type, type_result = validate_file_type(file)
            if not is_valid_type:
                return None, type_result
            
            mime_type = type_result
            
            # Determine file category
            file_category = get_file_category(mime_type)
            
            # Get file extension from filename
            original_filename = file.name
            file_extension = original_filename.split('.')[-1].lower() if '.' in original_filename else ''
            
            # Create Media instance
            media = Media(
                file=file,
                original_filename=original_filename,
                file_type=file_category,
                mime_type=mime_type,
                file_extension=file_extension,
                file_size=file.size,
                content_type=content_type,
                object_id=object_id,
                alt_text=alt_text,
                description=description,
                uploaded_by=user,
                status='active'
            )
            
            # Handle image-specific processing
            if file_category == 'image':
                try:
                    # Extract image dimensions
                    width, height = get_image_dimensions(file)
                    if width and height:
                        media.width = width
                        media.height = height
                except UnidentifiedImageError as e:
                    logger.warning(f"Could not process image: {str(e)}")
                    # Continue without dimensions
                except Exception as e:
                    logger.error(f"Error processing image: {str(e)}")
                    # Continue without dimensions
            
            # Save Media instance (this triggers S3 upload)
            try:
                media.save()
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                error_msg = e.response.get('Error', {}).get('Message', str(e))
                logger.error(f"S3 upload error during save: {error_code} - {error_msg}")
                return None, f"Failed to upload file to storage: {error_msg}"
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                logger.error(f"Error saving media instance: {str(e)}\n{error_traceback}")
                return None, f"Failed to save file: {str(e)}"
            
            return media, None
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            logger.error(f"S3 error during upload: {error_code} - {error_msg}")
            return None, f"Storage error: {error_msg}"
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Unexpected error during upload: {str(e)}\n{error_traceback}")
            return None, f"Error uploading file: {str(e)}"
    
    @staticmethod
    def delete_file(media_id, hard_delete: bool = False) -> Tuple[bool, Optional[str]]:
        """
        Delete a media file.
        
        Args:
            media_id: UUID of the Media instance
            hard_delete: If True, delete from S3 and database. If False, soft delete (set status to 'deleted')
        
        Returns:
            Tuple[bool, Optional[str]]: (success boolean, optional error message)
        """
        try:
            media = Media.objects.get(id=media_id)
            
            if hard_delete:
                # Delete file from S3
                try:
                    if media.file and media.file.name:
                        default_storage.delete(media.file.name)
                except ClientError as e:
                    error_msg = f"Error deleting file from S3: {str(e)}"
                    logger.error(error_msg)
                    return False, error_msg
                except Exception as e:
                    error_msg = f"Error deleting file: {str(e)}"
                    logger.error(error_msg)
                    return False, error_msg
                
                # Delete database record
                media.delete()
            else:
                # Soft delete: set status to 'deleted'
                media.status = 'deleted'
                media.save()
            
            return True, None
            
        except Media.DoesNotExist:
            return False, "Media not found"
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            error_msg = f"Error deleting media: {str(e)}"
            logger.error(f"{error_msg}\n{error_traceback}")
            return False, error_msg
    
    @staticmethod
    def update_file(
        media_id,
        file,
        user,
        alt_text=None,
        description=None
    ) -> Tuple[Optional[Media], Optional[str]]:
        """
        Update an existing media file by replacing it with a new file.
        Deletes the old file from S3 and uploads the new one.
        
        Args:
            media_id: UUID of the Media instance to update
            file: New file object to replace the existing one
            user: User instance making the update
            alt_text: Optional alt text for the file
            description: Optional description for the file
        
        Returns:
            Tuple[Optional[Media], Optional[str]]: (Media instance, None) on success or (None, error message) on failure
        """
        try:
            # Get existing media instance
            try:
                media = Media.objects.get(id=media_id, status='active')
            except Media.DoesNotExist:
                return None, "Media not found"
            
            # Validate file size
            is_valid_size, size_error = validate_file_size(file)
            if not is_valid_size:
                return None, size_error
            
            # Validate file type
            is_valid_type, type_result = validate_file_type(file)
            if not is_valid_type:
                return None, type_result
            
            mime_type = type_result
            
            # Determine file category
            file_category = get_file_category(mime_type)
            
            # Get file extension from filename
            original_filename = file.name
            file_extension = original_filename.split('.')[-1].lower() if '.' in original_filename else ''
            
            # Store old file path for deletion
            old_file_path = media.file.name if media.file and media.file.name else None
            
            # Update media instance with new file
            media.file = file
            media.original_filename = original_filename
            media.file_type = file_category
            media.mime_type = mime_type
            media.file_extension = file_extension
            media.file_size = file.size
            
            # Update optional fields if provided
            if alt_text is not None:
                media.alt_text = alt_text
            if description is not None:
                media.description = description
            
            # Reset image-specific fields
            media.width = None
            media.height = None
            
            # Handle image-specific processing
            if file_category == 'image':
                try:
                    # Extract image dimensions
                    width, height = get_image_dimensions(file)
                    if width and height:
                        media.width = width
                        media.height = height
                except UnidentifiedImageError as e:
                    logger.warning(f"Could not process image: {str(e)}")
                    # Continue without dimensions
                except Exception as e:
                    logger.error(f"Error processing image: {str(e)}")
                    # Continue without dimensions
            
            # Save Media instance (this triggers S3 upload of new file)
            try:
                media.save()
                
                # Delete old file from S3 after successful save
                if old_file_path:
                    try:
                        default_storage.delete(old_file_path)
                        logger.info(f"Deleted old file from S3: {old_file_path}")
                    except ClientError as e:
                        logger.warning(f"Could not delete old file from S3: {old_file_path}. Error: {str(e)}")
                    except Exception as e:
                        logger.warning(f"Error deleting old file: {old_file_path}. Error: {str(e)}")
                
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                error_msg = e.response.get('Error', {}).get('Message', str(e))
                logger.error(f"S3 upload error during update: {error_code} - {error_msg}")
                return None, f"Failed to upload file to storage: {error_msg}"
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                logger.error(f"Error updating media instance: {str(e)}\n{error_traceback}")
                return None, f"Failed to update file: {str(e)}"
            
            return media, None
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            logger.error(f"S3 error during update: {error_code} - {error_msg}")
            return None, f"Storage error: {error_msg}"
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            logger.error(f"Unexpected error during update: {str(e)}\n{error_traceback}")
            return None, f"Error updating file: {str(e)}"
    
    @staticmethod
    def get_media_for_object(content_type: str, object_id: int):
        """
        Get media records for a specific object using generic relationship.
        
        Args:
            content_type: ContentType string in format 'app.Model'
            object_id: Object ID
        
        Returns:
            QuerySet: Media records filtered by content_type, object_id, and active status
        """
        try:
            # Parse content_type string (format: 'app.Model')
            app_label, model = content_type.split('.')
            
            # Get ContentType instance (cached)
            content_type_obj = get_content_type_cached(app_label, model)
            
            # Query Media records
            queryset = Media.objects.filter(
                content_type=content_type_obj,
                object_id=object_id,
                status='active'
            )
            
            return queryset
            
        except ContentType.DoesNotExist:
            return Media.objects.none()
        except ValueError:
            # Invalid content_type format
            return Media.objects.none()
        except Exception as e:
            logger.error(f"Error getting media for object: {str(e)}")
            return Media.objects.none()
    
    @staticmethod
    def confirm_s3_upload(
        s3_key: str,
        original_filename: str,
        file_type: str,
        file_size: int,
        user,
        content_type=None,
        object_id=None,
        alt_text=None,
        description=None
    ) -> Tuple[Optional[Media], Optional[str]]:
        """
        Confirm an S3 upload and create Media instance.
        Uses head_object for size check; optionally validates type via range request.
        Does not download the full file (no image dimensions extracted here).
        
        Args:
            s3_key: S3 key/path of the uploaded file
            original_filename: Original filename
            file_type: MIME type
            file_size: File size in bytes
            user: User instance
            content_type: Optional ContentType instance
            object_id: Optional object ID
            alt_text: Optional alt text
            description: Optional description
        
        Returns:
            Tuple[Optional[Media], Optional[str]]: (Media instance, None) on success or (None, error message) on failure
        """
        try:
            # Get AWS configuration
            aws_access_key_id = getattr(settings, 'AWS_ACCESS_KEY_ID', '')
            aws_secret_access_key = getattr(settings, 'AWS_SECRET_ACCESS_KEY', '')
            aws_region = getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1')
            bucket_name = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')
            
            if not all([aws_access_key_id, aws_secret_access_key, bucket_name, s3_key]):
                error_msg = "Missing required AWS configuration or S3 key"
                logger.error(
                    "S3 upload confirmation failed: Missing configuration",
                    extra={
                        'error_type': 'ConfigurationError',
                        's3_key': s3_key,
                        's3_bucket': bucket_name,
                        'user_id': user.id if user else None,
                        'operation': 'confirm_s3_upload',
                        'stack_trace': traceback.format_exc()
                    }
                )
                return None, error_msg

            s3_client = get_s3_client()

            # Verify file exists and get size from head_object (single round-trip; no full download)
            try:
                head_response = s3_client.head_object(Bucket=bucket_name, Key=s3_key)
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                if error_code == '404':
                    error_msg = f"File not found in S3: {s3_key}"
                else:
                    error_msg = f"S3 error: {e.response.get('Error', {}).get('Message', str(e))}"
                
                logger.error(
                    "S3 upload confirmation failed: File not found in S3",
                    extra={
                        'error_type': 'S3FileNotFound',
                        'error_code': error_code,
                        's3_key': s3_key,
                        's3_bucket': bucket_name,
                        'user_id': user.id if user else None,
                        'file_name': original_filename,
                        'operation': 'confirm_s3_upload',
                        'stack_trace': traceback.format_exc()
                    }
                )
                return None, error_msg

            # Size check using ContentLength from head (avoid full download)
            content_length = head_response.get('ContentLength')
            if content_length is not None and content_length != file_size:
                    error_msg = f"File size mismatch: expected {file_size}, got {content_length}"
                    logger.error(
                        "S3 upload confirmation failed: File size mismatch",
                        extra={
                            'error_type': 'FileSizeMismatch',
                            's3_key': s3_key,
                            'expected_size': file_size,
                            'actual_size': content_length,
                            'user_id': user.id if user else None,
                            'file_name': original_filename,
                            'operation': 'confirm_s3_upload',
                            'stack_trace': traceback.format_exc()
                        }
                    )
                    try:
                        s3_client.delete_object(Bucket=bucket_name, Key=s3_key)
                    except Exception:
                        pass
                    return None, error_msg

            # Optional: validate file type via range request (first 8KB). Disabled by default to avoid
            # a second S3 round-trip and reduce confirm-upload latency (5–14s -> ~1–2s typical).
            # Set MEDIA_CONFIRM_VALIDATE_S3_TYPE = True in settings for stricter server-side validation.
            mime_type = file_type
            if getattr(settings, 'MEDIA_CONFIRM_VALIDATE_S3_TYPE', False):
                try:
                    s3_response = s3_client.get_object(
                        Bucket=bucket_name, Key=s3_key, Range='bytes=0-8191'
                    )
                    chunk = s3_response['Body'].read()
                    file_obj = io.BytesIO(chunk)
                    file_obj.name = original_filename
                    file_obj.size = file_size
                    is_valid_type, type_result = validate_file_type(file_obj)
                    if is_valid_type:
                        expected_category = get_file_category(file_type)
                        actual_category = get_file_category(type_result)
                        if expected_category == actual_category:
                            mime_type = type_result
                except ClientError:
                    pass  # Use client-provided file_type if range request fails
                except Exception:
                    pass  # Use client-provided file_type on any validation error

            file_category = get_file_category(mime_type)
            file_extension = original_filename.split('.')[-1].lower() if '.' in original_filename else ''

            # Create Media instance without full download (no image dimensions).
            # Set file.name directly to the S3 key to avoid uploading an empty
            # ContentFile and then patching with raw SQL.
            media = Media(
                original_filename=original_filename,
                file_type=file_category,
                mime_type=mime_type,
                file_extension=file_extension,
                file_size=file_size,
                content_type=content_type,
                object_id=object_id,
                alt_text=alt_text,
                description=description,
                uploaded_by=user,
                status='active',
                width=None,
                height=None,
                s3_key=s3_key,
                s3_bucket=bucket_name
            )
            # Assign the S3 key directly to the FileField's name attribute
            # without triggering a storage save (no empty file uploaded).
            media.file.name = s3_key

            try:
                media.save()
            except Exception as e:
                error_msg = f"Failed to create Media record: {str(e)}"
                logger.error(
                    "S3 upload confirmation failed: Media creation error",
                    extra={
                        'error_type': 'MediaCreationError',
                        'error_message': str(e),
                        's3_key': s3_key,
                        's3_bucket': bucket_name,
                        'user_id': user.id if user else None,
                        'file_name': original_filename,
                        'operation': 'confirm_s3_upload',
                        'stack_trace': traceback.format_exc()
                    }
                )
                try:
                    s3_client.delete_object(Bucket=bucket_name, Key=s3_key)
                except Exception:
                    pass
                return None, error_msg

            return media, None
                
        except Exception as e:
            logger.error(
                "S3 upload confirmation failed: Unexpected error",
                extra={
                    'error_type': 'UnexpectedError',
                    'error_message': str(e),
                    's3_key': s3_key,
                    'user_id': user.id if user else None,
                    'file_name': original_filename,
                    'operation': 'confirm_s3_upload',
                    'stack_trace': traceback.format_exc()
                }
            )
            return None, f"Error confirming upload: {str(e)}"
