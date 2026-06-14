from django.urls import path
from incidents.views import (
    IncidentListCreateAPIView,
    IncidentDetailAPIView,
    IncidentStatusUpdateAPIView,
    IncidentStartAPIView,
    IncidentSubmitForReviewAPIView,
    IncidentSendBackAPIView,
    IncidentPMSignoffAPIView,
    IncidentAcknowledgeAPIView,
    IncidentResidentContextAPIView,
)

urlpatterns = [
    path('', IncidentListCreateAPIView.as_view(), name='incident-list-create'),
    path('resident-context/<uuid:resident_uuid>/', IncidentResidentContextAPIView.as_view(), name='incident-resident-context'),
    path('<uuid:uuid>/start/', IncidentStartAPIView.as_view(), name='incident-start'),
    path('<uuid:uuid>/submit-for-review/', IncidentSubmitForReviewAPIView.as_view(), name='incident-submit-for-review'),
    path('<uuid:uuid>/send-back/', IncidentSendBackAPIView.as_view(), name='incident-send-back'),
    path('<uuid:uuid>/pm-signoff/', IncidentPMSignoffAPIView.as_view(), name='incident-pm-signoff'),
    path('<uuid:uuid>/acknowledge/', IncidentAcknowledgeAPIView.as_view(), name='incident-acknowledge'),
    path('<uuid:uuid>/', IncidentDetailAPIView.as_view(), name='incident-detail'),
    path('<uuid:uuid>/status/', IncidentStatusUpdateAPIView.as_view(), name='incident-status-update'),
]
