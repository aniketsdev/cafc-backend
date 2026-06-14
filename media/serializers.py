from rest_framework import serializers
from django.contrib.contenttypes.models import ContentType
from rest_framework.exceptions import ValidationError

from .models import Media
from .services import MediaService
from .utils import get_content_type_cached


def _role_aware_metadata(obj, request):
    metadata = dict(obj.metadata or {})
    role_type = getattr(
        getattr(getattr(request, "user", None), "role", None),
        "type",
        "",
    ).upper()
    role_signature = (metadata.get("role_signatures") or {}).get(role_type)
    if role_type in ("GUARDIAN", "AGENT") and role_signature:
        metadata.update(role_signature)
    return metadata


class MediaUploadSerializer(serializers.Serializer):
    """Serializer for handling file uploads"""

    file = serializers.FileField(required=True)
    content_type_model = serializers.CharField(required=False, allow_blank=True)
    content_type_app = serializers.CharField(required=False, allow_blank=True)
    object_uuid = serializers.UUIDField(required=False, allow_null=True)
    alt_text = serializers.CharField(required=False, allow_blank=True, max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        """Validate that if content_type fields are provided, both app and model must be present.
        Resolve object_uuid to internal object_id."""
        content_type_model = attrs.get('content_type_model')
        content_type_app = attrs.get('content_type_app')

        if (content_type_model and not content_type_app) or (content_type_app and not content_type_model):
            raise ValidationError(
                "Both content_type_app and content_type_model must be provided together"
            )

        # Resolve object_uuid to object_id
        object_uuid = attrs.get('object_uuid')
        if object_uuid and content_type_app and content_type_model:
            try:
                ct = get_content_type_cached(content_type_app, content_type_model)
                model_class = ct.model_class()
                if model_class:
                    obj = model_class.objects.filter(uuid=object_uuid).first()
                    if not obj:
                        raise ValidationError(
                            f"Object with uuid {object_uuid} not found for {content_type_app}.{content_type_model}"
                        )
                    attrs['_resolved_object_id'] = obj.id
            except ContentType.DoesNotExist:
                pass  # Will be caught in create()

        return attrs

    def create(self, validated_data):
        """Create Media instance using MediaService"""
        file = validated_data['file']
        user = self.context['request'].user

        # Resolve ContentType if provided
        content_type = None
        object_id = validated_data.get('_resolved_object_id')

        content_type_app = validated_data.get('content_type_app')
        content_type_model = validated_data.get('content_type_model')

        if content_type_app and content_type_model:
            try:
                content_type = get_content_type_cached(content_type_app, content_type_model)
            except ContentType.DoesNotExist:
                raise ValidationError(
                    f"ContentType not found for {content_type_app}.{content_type_model}"
                )

        # Call MediaService to upload file
        media, error = MediaService.upload_file(
            file=file,
            user=user,
            content_type=content_type,
            object_id=object_id,
            alt_text=validated_data.get('alt_text'),
            description=validated_data.get('description')
        )

        if error:
            raise ValidationError(error)

        return media


class MediaSerializer(serializers.ModelSerializer):
    """Serializer for retrieving media information"""
    
    file_url = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()
    signed = serializers.SerializerMethodField()
    metadata = serializers.SerializerMethodField()
    
    class Meta:
        model = Media
        fields = [
            'id',
            'file_url',
            'original_filename',
            'file_type',
            'mime_type',
            'file_extension',
            'file_size',
            'width',
            'height',
            'alt_text',
            'description',
            'status',
            'metadata',
            'signed',
            'uploaded_by',
            'uploaded_at',
            'updated_at',
        ]
        read_only_fields = fields
    
    def get_file_url(self, obj):
        """Get signed URL for the file"""
        try:
            return obj.get_file_url()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error generating file URL for media {obj.id}: {str(e)}")
            return None
    
    def get_uploaded_by(self, obj):
        """Get nested user info"""
        if obj.uploaded_by:
            return {
                'uuid': str(obj.uploaded_by.uuid),
                'email': obj.uploaded_by.email,
                'first_name': obj.uploaded_by.first_name,
                'last_name': obj.uploaded_by.last_name,
            }
        return None
    
    def get_signed(self, obj):
        return self.get_metadata(obj).get("signed", False)

    def get_metadata(self, obj):
        return _role_aware_metadata(obj, self.context.get("request"))


class MediaUpdateSerializer(serializers.Serializer):
    """Serializer for updating existing media files"""
    
    file = serializers.FileField(required=True)
    alt_text = serializers.CharField(required=False, allow_blank=True, max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    
    def update(self, instance, validated_data):
        """Update Media instance using MediaService"""
        file = validated_data['file']
        user = self.context['request'].user
        
        # Call MediaService to update file
        media, error = MediaService.update_file(
            media_id=instance.id,
            file=file,
            user=user,
            alt_text=validated_data.get('alt_text'),
            description=validated_data.get('description')
        )
        
        if error:
            raise ValidationError(error)
        
        return media


class MediaListSerializer(serializers.ModelSerializer):
    """Lighter serializer for list views"""

    file_url = serializers.SerializerMethodField()
    description = serializers.CharField(read_only=True)
    metadata = serializers.SerializerMethodField()

    class Meta:
        model = Media
        fields = [
            'id',
            'file_url',
            'original_filename',
            'file_type',
            'mime_type',
            'file_extension',
            'file_size',
            'width',
            'height',
            'alt_text',
            'description',
            'status',
            'metadata',
            'uploaded_by',
            'uploaded_at',
            'updated_at',
        ]
        read_only_fields = ("id", "uploaded_at")


    def get_file_url(self, obj):
        """Get signed URL for the file"""
        return obj.get_file_url()

    def get_metadata(self, obj):
        return _role_aware_metadata(obj, self.context.get("request"))


#==========================================================
# media/serializers.py
class MediaMinimalSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = Media
        fields = [
            'id',
            'file_url',
            'original_filename',
            'file_type',
            'mime_type',
            'file_extension',
            'file_size',
            'alt_text',
            'description',
            'status',
            'metadata',
            'uploaded_at',
        ]

    def get_file_url(self, obj):
        return obj.get_file_url()
