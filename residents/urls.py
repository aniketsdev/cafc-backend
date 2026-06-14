from django.urls import path
from residents.views import (

    CarePlanItemListCreateAPIView,
    CarePlanItemDetailAPIView,
    CarePlanItemArchiveAPIView,

    CarePlanDailyLogListCreateAPIView,
    CarePlanDailyLogDetailAPIView,
    CarePlanDailyLogArchiveAPIView,
    CarePlanArchivedDailyLogListAPIView,

    CarePlanReportCreateView,
    CarePlanReportRetrieveDeleteView,

    ResidentScheduledFormListCreateAPIView,
    ResidentScheduledFormDestroyAPIView,
)

urlpatterns = [

    # --------------------------------------------------
    # Resident Scheduled Forms (e.g. "Schedule 30-Day ISA")
    # MUST be above <str:uuid>/ catch-all to avoid 405 routing conflict
    # --------------------------------------------------
    path(
        "resident-scheduled-forms/",
        ResidentScheduledFormListCreateAPIView.as_view(),
        name="resident-scheduled-form-list-create",
    ),
    path(
        "resident-scheduled-forms/<int:pk>/",
        ResidentScheduledFormDestroyAPIView.as_view(),
        name="resident-scheduled-form-destroy",
    ),

    # --------------------------------------------------
    # Care Plan Monthly Reports (MUST BE ABOVE <str:uuid>/ catch-all)
    # --------------------------------------------------
    path(
        "care-plan-reports/",
        CarePlanReportCreateView.as_view(),
        name="care-plan-report-create",
    ),
    path(
        "care-plan-reports/<str:uuid>/",
        CarePlanReportRetrieveDeleteView.as_view(),
        name="care-plan-report-retrieve-delete",
    ),

    # --------------------------------------------------
    # Daily Logs (Tracking) → Put BEFORE dynamic UUID
    # --------------------------------------------------
    path("daily-logs/archived/", CarePlanArchivedDailyLogListAPIView.as_view()),
    path("daily-logs/", CarePlanDailyLogListCreateAPIView.as_view()),
    path("daily-logs/<str:uuid>/archive/", CarePlanDailyLogArchiveAPIView.as_view()),
    path("daily-logs/<str:uuid>/", CarePlanDailyLogDetailAPIView.as_view()),

    # --------------------------------------------------
    # Care Plan Items (ADLs / Goals) — <str:uuid>/ catch-all LAST
    # --------------------------------------------------
    path("", CarePlanItemListCreateAPIView.as_view(), name="care-plan-item-list-create"),
    path("<str:uuid>/archive/", CarePlanItemArchiveAPIView.as_view(), name="care-plan-item-archive"),
    path("<str:uuid>/", CarePlanItemDetailAPIView.as_view(), name="care-plan-item-detail"),

]
