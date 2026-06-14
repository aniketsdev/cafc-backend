from django.urls import path
from .views import ProviderAPIView, ProviderDetailAPIView

urlpatterns = [
    # GET (list) + POST (create)
    path("", ProviderAPIView.as_view(), name="provider-list-create"),

    # GET (detail) + PUT (update) + DELETE (soft-delete)
    path("<uuid:uuid>/", ProviderDetailAPIView.as_view(), name="provider-detail"),
]
