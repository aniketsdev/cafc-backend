import magic
import uuid
from django.conf import settings
from django.utils import timezone
from PIL import Image
import io
import logging
import traceback
from typing import Tuple, Optional, Dict
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def get_content_type_cached(app_label: str, model: str):
    """
    Cached ContentType lookup. Uses Django's built-in ContentType cache
    (get_by_natural_key) which avoids hitting the DB on repeated calls
    with the same app_label/model within the same process.

    Returns:
        ContentType instance

    Raises:
        ContentType.DoesNotExist if not found
    """
    from django.contrib.contenttypes.models import ContentType
    return ContentType.objects.get_by_natural_key(app_label, model.lower())


def validate_file_size(file) -> Tuple[bool, str]:
    """
    Check if uploaded file size is within MAX_UPLOAD_SIZE limit.
    
    Returns:
        Tuple[bool, str]: (success boolean, error message)
    """
    max_size = getattr(settings, 'MAX_UPLOAD_SIZE', 10 * 1024 * 1024)  # Default 10MB
    
    if file.size > max_size:
        max_size_mb = max_size / (1024 * 1024)
        return False, f"File size exceeds maximum allowed size of {max_size_mb}MB"
    
    return True, ""


def validate_file_type(file) -> Tuple[bool, str]:
    """
    Use python-magic to detect actual MIME type from file content.
    Verify against ALLOWED_FILE_TYPES.
    
    Note: Resets file pointer after reading.
    
    Returns:
        Tuple[bool, str]: (success boolean, mime_type or error message)
    """
    allowed_types = getattr(settings, 'ALLOWED_FILE_TYPES', [])
    
    if not allowed_types:
        return False, "No allowed file types configured. Please contact administrator."
    
    # Save current position
    current_position = file.tell()
    
    try:
        # Read file content to detect MIME type
        file.seek(0)
        file_content = file.read(1024)  # Read first 1024 bytes for MIME detection
        
        if not file_content:
            file.seek(current_position)
            return False, "File appears to be empty or could not be read."
        
        file.seek(0)  # Reset to beginning
        
        # Detect MIME type using python-magic
        try:
            mime_type = magic.from_buffer(file_content, mime=True)
        except Exception as magic_error:
            # python-magic might fail if libmagic is not installed
            file.seek(current_position)
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Magic library error: {str(magic_error)}")
            return False, f"Could not detect file type. Please ensure the file is valid. Error: {str(magic_error)}"
        
        if not mime_type:
            file.seek(current_position)
            return False, "Could not detect file type. Please ensure the file is valid."
        
        # Reset file pointer to original position
        file.seek(current_position)
        
        if mime_type not in allowed_types:
            return False, f"File type '{mime_type}' is not allowed. Allowed types: {', '.join(allowed_types)}"
        
        return True, mime_type
        
    except Exception as e:
        # Reset file pointer on error
        try:
            file.seek(current_position)
        except:
            pass
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error in validate_file_type: {str(e)}")
        return False, f"Error detecting file type: {str(e)}"


def get_file_category(mime_type: str) -> str:
    """
    Map MIME type to category.
    
    Returns:
        str: Category (image, video, audio, document, other)
    """
    if mime_type.startswith('image/'):
        return 'image'
    elif mime_type.startswith('video/'):
        return 'video'
    elif mime_type.startswith('audio/'):
        return 'audio'
    elif mime_type in [
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-powerpoint',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    ]:
        return 'document'
    else:
        return 'other'


def get_image_dimensions(image_file) -> Tuple[Optional[int], Optional[int]]:
    """
    Open image with Pillow, extract width and height.
    Reset file pointer, return tuple of dimensions or None values on error.
    
    Returns:
        Tuple[Optional[int], Optional[int]]: (width, height) or (None, None) on error
    """
    try:
        # Save current position
        current_position = image_file.tell()
        image_file.seek(0)
        
        # Open image with Pillow
        image = Image.open(image_file)
        width, height = image.size
        
        # Reset file pointer
        image_file.seek(current_position)
        
        return width, height
        
    except Exception as e:
        # Reset file pointer on error
        try:
            image_file.seek(current_position)
        except:
            pass
        return None, None

def generate_signed_url(s3_key: str) -> Optional[str]:
    """
    Generate a signed URL for an S3 object using boto3.
    Uses cached S3 client to avoid per-call connection overhead.
    
    Args:
        s3_key: The S3 key/path of the file
    
    Returns:
        str: Signed URL or None on error
    """
    try:
        bucket_name = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')
        expire_seconds = getattr(settings, 'AWS_QUERYSTRING_EXPIRE', 3600)
        
        if not bucket_name or not s3_key:
            missing = []
            if not bucket_name:
                missing.append('AWS_STORAGE_BUCKET_NAME')
            if not s3_key:
                missing.append('s3_key')
            logger.error(f"Missing required configuration for signed URL generation: {', '.join(missing)}")
            return None

        from .s3_client import get_s3_client
        s3_client = get_s3_client()
        
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': bucket_name,
                'Key': s3_key
            },
            ExpiresIn=expire_seconds
        )
        
        return url
    except Exception as e:
        import logging
        import traceback
        logger = logging.getLogger(__name__)
        error_traceback = traceback.format_exc()
        logger.error(f"Error generating signed URL for key '{s3_key}': {str(e)}\n{error_traceback}")
        return None


def generate_presigned_upload_url(
    file_name: str,
    file_type: str,
    file_size: int,
    user_id: Optional[str] = None,
    content_type_app: Optional[str] = None,
    content_type_model: Optional[str] = None,
    object_id: Optional[int] = None
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Generate presigned S3 URL for direct client upload.
    
    Args:
        file_name: Original filename
        file_type: MIME type (e.g., 'image/jpeg')
        file_size: File size in bytes
        user_id: Optional user ID for context
        content_type_app: Optional app name for content type
        content_type_model: Optional model name for content type
        object_id: Optional object ID
    
    Returns:
        Tuple[Optional[Dict], Optional[str]]: 
        (presigned_url_data, None) on success or (None, error_message) on failure
    """
    try:
        import boto3
        
        # Get AWS configuration
        aws_access_key_id = getattr(settings, 'AWS_ACCESS_KEY_ID', '')
        aws_secret_access_key = getattr(settings, 'AWS_SECRET_ACCESS_KEY', '')
        aws_region = getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1')
        bucket_name = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')
        max_upload_size = getattr(settings, 'MAX_UPLOAD_SIZE', 10 * 1024 * 1024)
        allowed_types = getattr(settings, 'ALLOWED_FILE_TYPES', [])
        
        # Validate configuration
        if not all([aws_access_key_id, aws_secret_access_key, bucket_name]):
            missing = []
            if not aws_access_key_id:
                missing.append('AWS_ACCESS_KEY_ID')
            if not aws_secret_access_key:
                missing.append('AWS_SECRET_ACCESS_KEY')
            if not bucket_name:
                missing.append('AWS_STORAGE_BUCKET_NAME')
            
            error_msg = f"Missing required AWS configuration: {', '.join(missing)}"
            logger.error(
                "Presigned URL generation failed: Missing AWS configuration",
                extra={
                    'error_type': 'ConfigurationError',
                    'missing_config': missing,
                    'operation': 'generate_presigned_upload_url',
                    'stack_trace': traceback.format_exc()
                }
            )
            return None, error_msg
        
        # Validate file size
        if file_size > max_upload_size:
            max_size_mb = max_upload_size / (1024 * 1024)
            error_msg = f"File size ({file_size} bytes) exceeds maximum allowed size of {max_size_mb}MB"
            logger.error(
                "Presigned URL generation failed: File too large",
                extra={
                    'error_type': 'FileSizeError',
                    'file_name': file_name,
                    'file_size': file_size,
                    'max_size': max_upload_size,
                    'user_id': user_id,
                    'operation': 'generate_presigned_upload_url',
                    'stack_trace': traceback.format_exc()
                }
            )
            return None, error_msg
        
        # Validate file type
        if allowed_types and file_type not in allowed_types:
            error_msg = f"File type '{file_type}' is not allowed. Allowed types: {', '.join(allowed_types)}"
            logger.error(
                "Presigned URL generation failed: Invalid file type",
                extra={
                    'error_type': 'FileTypeError',
                    'file_name': file_name,
                    'file_type': file_type,
                    'allowed_types': allowed_types,
                    'user_id': user_id,
                    'operation': 'generate_presigned_upload_url',
                    'stack_trace': traceback.format_exc()
                }
            )
            return None, error_msg
        
        # Generate unique S3 key
        now = timezone.now()
        file_extension = file_name.split('.')[-1].lower() if '.' in file_name else ''
        unique_filename = f"{uuid.uuid4()}-{file_name}"
        s3_key = f"{now.year}/{now.month:02d}/{now.day:02d}/{unique_filename}"
        
        from .s3_client import get_s3_client
        s3_client = get_s3_client()
        
        # Generate presigned POST URL (allows direct upload)
        # Expires in 15 minutes (900 seconds)
        expiration = 900
        
        presigned_post = s3_client.generate_presigned_post(
            Bucket=bucket_name,
            Key=s3_key,
            Fields={
                'Content-Type': file_type,
            },
            Conditions=[
                {'Content-Type': file_type},
                ['content-length-range', 1, max_upload_size],
            ],
            ExpiresIn=expiration
        )
        
        return {
            'upload_url': presigned_post['url'],
            's3_key': s3_key,
            'fields': presigned_post['fields'],
            'expires_in': expiration
        }, None
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_msg = e.response.get('Error', {}).get('Message', str(e))
        
        logger.error(
            "Presigned URL generation failed: S3 error",
            extra={
                'error_type': 'S3Error',
                'error_code': error_code,
                'error_message': error_msg,
                'file_name': file_name,
                'file_type': file_type,
                'file_size': file_size,
                'user_id': user_id,
                's3_bucket': bucket_name,
                'operation': 'generate_presigned_upload_url',
                'stack_trace': traceback.format_exc()
            }
        )
        return None, f"S3 error: {error_msg}"
        
    except Exception as e:
        logger.error(
            "Presigned URL generation failed: Unexpected error",
            extra={
                'error_type': 'UnexpectedError',
                'error_message': str(e),
                'file_name': file_name,
                'file_type': file_type,
                'file_size': file_size,
                'user_id': user_id,
                'operation': 'generate_presigned_upload_url',
                'stack_trace': traceback.format_exc()
            }
        )
        return None, f"Error generating presigned URL: {str(e)}"