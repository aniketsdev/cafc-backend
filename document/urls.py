from django.urls import path

from document.views import (
    ConsentFormCreateAPIView,
    ConsentFormListAPIView,
    ConsentFormDetailAPIView,
    ConsentFormShareAPIView,
    share_media_only,
)

urlpatterns = [
    path(
        "consent-forms/",
        ConsentFormCreateAPIView.as_view(),
        name="create-consent-form",
    ),
    path(
        "consent-forms/list/",
        ConsentFormListAPIView.as_view(),
        name="list-consent-forms",
    ),
    path(
        "consent-forms/<uuid:uuid>/",
        ConsentFormDetailAPIView.as_view(),  # ✅ GET + PUT + DELETE
        name="consent-form-detail-update",
    ),
    path(
        "consent-forms/<uuid:uuid>/share/",
        ConsentFormShareAPIView.as_view(),
        name="share-consent-form",
    ),
    path(
        "media/<uuid:media_uuid>/share/",
        share_media_only,
        name="share-media-only",
    ),
]
