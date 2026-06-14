import uuid
from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone
from storages.backends.s3boto3 import S3Boto3Storage
from .utils import generate_signed_url

def default_media_metadata():
    return {
        "signed": False,
        "signed_by": None,
        "signed_date": None,
    }



def upload_to_path(instance, filename):
    """Generate upload path: year/month/day/filename"""
    now = timezone.now()
    return f"{now.year}/{now.month:02d}/{now.day:02d}/{filename}"


class Media(models.Model):
    """Centralized media model for file storage with S3 integration"""
    
    FILE_TYPE_CHOICES = [
        ('image', 'Image'),
        ('document', 'Document'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('other', 'Other'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('deleted', 'Deleted'),
        ('archived', 'Archived'),
    ]
    
    # Primary Key
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )
    
    # File Information - EXPLICITLY USE S3 STORAGE
    file = models.FileField(
        upload_to=upload_to_path,
        storage=S3Boto3Storage(),  # Explicitly use S3 storage
        max_length=500
    )
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(
        max_length=20,
        choices=FILE_TYPE_CHOICES
    )
    mime_type = models.CharField(max_length=100)
    file_extension = models.CharField(max_length=10)
    file_size = models.PositiveIntegerField(help_text="File size in bytes")
    
    # S3 Specific Fields
    s3_key = models.CharField(
        max_length=500,
        blank=True,
        help_text="Full S3 path/key for the file"
    )
    s3_bucket = models.CharField(
        max_length=255,
        blank=True,
        help_text="S3 bucket name"
    )
    
    # Generic Relationship Fields
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='media_items'
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    
    # Image Specific Fields
    width = models.PositiveIntegerField(null=True, blank=True, help_text="Image width in pixels")
    height = models.PositiveIntegerField(null=True, blank=True, help_text="Image height in pixels")
    
    metadata = models.JSONField(
    default=default_media_metadata,
    blank=True,
    help_text="Extra metadata like signing info"
)


    # Metadata Fields
    alt_text = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active'
    )
    
    # Tracking Fields
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='uploaded_media'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'media'
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
            models.Index(fields=['file_type']),
            models.Index(fields=['status']),
        ]
    
    def __str__(self):
        return f"{self.original_filename} ({self.file_type})"
    
    def get_file_url(self):
        """Generate signed URL for the file"""
        try:
            if self.file and self.file.name:
                # Use utility function to generate signed URL
                return generate_signed_url(self.file.name)
            return None
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error generating file URL for media {self.id}: {str(e)}")
            return None
    
    def save(self, *args, **kwargs):
        """Override save to auto-populate S3 fields"""
        if self.file and self.file.name:
            self.s3_key = self.file.name
            self.s3_bucket = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')
        super().save(*args, **kwargs)

    def mark_signed(self, user, signature_data=None, signer_name=None):
        # Include "updated_at" in update_fields so Django's auto_now actually
        # runs — without this, update_fields=["metadata"] silently bypasses
        # auto_now and the field stays frozen at upload time.
        now = timezone.now()
        self.metadata = {
            **self.metadata,
            "signed": True,
            "signed_by": str(user.id),
            "signed_date": now.isoformat(),
            **({"signature_data": signature_data} if signature_data else {}),
            **({"signer_name": signer_name} if signer_name else {}),
        }
        self.updated_at = now
        self.save(update_fields=["metadata", "updated_at"])

    def mark_unsigned(self):
        # Same fix as mark_signed — advance updated_at explicitly because
        # update_fields would otherwise bypass auto_now.
        self.metadata = {
            **self.metadata,
            "signed": False,
            "signed_by": None,
            "signed_date": None
        }
        self.updated_at = timezone.now()
        self.save(update_fields=["metadata", "updated_at"])

    @property
    def is_signed(self):
        return self.metadata.get("signed", False)