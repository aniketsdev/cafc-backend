from django.urls import path
from .views import (
    MediaUploadAPIView,
    MediaRetrieveAPIView,
    MediaDeleteAPIView,
    MediaUpdateAPIView,
    MediaListAPIView,
    MediaSignAPIView,
    GeneratePresignedUploadURLAPIView,
    ConfirmUploadAPIView
)

app_name = 'media'

urlpatterns = [
    # POST /api/media/generate-upload-url/ - Generate presigned S3 URL for direct upload
    path('generate-upload-url/', GeneratePresignedUploadURLAPIView.as_view(), name='generate-upload-url'),
    
    # POST /api/media/confirm-upload/ - Confirm S3 upload and create Media record
    path('confirm-upload/', ConfirmUploadAPIView.as_view(), name='confirm-upload'),
    
    # POST /api/media/upload/ - DEPRECATED: proxies file through Django (slow).
    # Use presigned URL flow: generate-upload-url/ → S3 direct → confirm-upload/
    path('upload/', MediaUploadAPIView.as_view(), name='upload'),
    
    # POST /api/media/<uuid:pk>/sign/ - Sign a document
    path('<uuid:pk>/sign/', MediaSignAPIView.as_view(), name='sign'),

    # DELETE /api/media/<uuid:pk>/delete/
    # Must come before the retrieve pattern to match correctly
    path('<uuid:pk>/delete/', MediaDeleteAPIView.as_view(), name='delete'),
    
    # PUT /api/media/<uuid:pk>/update/ - Update existing media file
    path('<uuid:pk>/update/', MediaUpdateAPIView.as_view(), name='update'),
    
    # GET /api/media/<uuid:pk>/
    path('<uuid:pk>/', MediaRetrieveAPIView.as_view(), name='retrieve'),
    
    # GET /api/media/list/ or GET /api/media/
    path('list/', MediaListAPIView.as_view(), name='list'),
    path('', MediaListAPIView.as_view(), name='list-root'),
]
