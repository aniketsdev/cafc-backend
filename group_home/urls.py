from django.urls import path
from group_home.views import (
    GroupHomeListCreateAPIView,
    GroupHomeDetailAPIView,
    GroupHomeStatusAPIView,
    GroupHomeUsersAPIView,
    GroupHomeAssignmentListCreateAPIView,
    GroupHomeAssignmentDeleteAPIView,
    GroupHomeCanDeleteAPIView,
)

urlpatterns = [
    path('', GroupHomeListCreateAPIView.as_view(), name='group-home-list-create'),
    path("<str:uuid>/assignments/", GroupHomeAssignmentListCreateAPIView.as_view(), name="group-home-assignments"),
    path("<str:uuid>/assignments/<uuid:assignment_uuid>/", GroupHomeAssignmentDeleteAPIView.as_view(), name="group-home-assignment-detail"),
    path("<str:uuid>/can-delete/", GroupHomeCanDeleteAPIView.as_view(), name="group-home-can-delete"),
    path('<str:uuid>/', GroupHomeDetailAPIView.as_view(), name='group-home-detail'),
    path("<str:uuid>/status", GroupHomeStatusAPIView.as_view(), name="group-home-status"),
    path("<str:uuid>/users/", GroupHomeUsersAPIView.as_view(), name="group_home_users"),
]
