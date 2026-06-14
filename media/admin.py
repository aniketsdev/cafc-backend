from django.contrib import admin
from django.utils.html import format_html
from .models import Media


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = [
        'id',
        'original_filename',
        'file_type',
        'file_size',
        'status',
        'uploaded_by',
        'uploaded_at'
    ]
    list_filter = ['file_type', 'status', 'uploaded_at']
    search_fields = ['original_filename', 'alt_text', 'description']
    readonly_fields = [
        'id',
        's3_key',
        's3_bucket',
        'uploaded_at',
        'updated_at',
        'file_url_display'
    ]
    
    fieldsets = (
        ('File Information', {
            'fields': ('file', 'original_filename', 'file_type', 'mime_type', 'file_extension', 'file_size')
        }),
        ('S3 Information', {
            'fields': ('s3_key', 's3_bucket', 'file_url_display')
        }),
        ('Generic Relationship', {
            'fields': ('content_type', 'object_id'),
            'classes': ('collapse',)
        }),
        ('Image Information', {
            'fields': ('width', 'height'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('alt_text', 'description', 'status')
        }),
        ('Tracking', {
            'fields': ('uploaded_by', 'uploaded_at', 'updated_at')
        }),
    )
    
    def file_url_display(self, obj):
        """Display file URL in admin"""
        url = obj.get_file_url()
        if url:
            return format_html('<a href="{}" target="_blank">View File</a>', url)
        return 'N/A'
    file_url_display.short_description = 'File URL'
