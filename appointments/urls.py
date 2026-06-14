from django.urls import path
from .views import AppointmentAPIView, AppointmentDetailAPIView,AppointmentStatusUpdateAPIView

urlpatterns = [
    path("", AppointmentAPIView.as_view(), name="create-appointment"), # POST + GET
    path("<uuid:uuid>/",AppointmentDetailAPIView.as_view(),name="appointment-detail"), # GET + PUT + DELETE
    path("<uuid:uuid>/status/",AppointmentStatusUpdateAPIView.as_view(),name="appointment-status-update"), # PATCH
]
