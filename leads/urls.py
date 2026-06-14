from django.urls import path
from leads.views import (LeadAPIView, LeadDetailAPIView,
                         LeadRefreshStatusAPIView,
                         CompleteOnboardingAPIView,
                         MoveOutAPIView,
                         ReAdmitAPIView,
                         ResidentListAPIView,RejectReferralAPIView,TransferResidentAPIView)

urlpatterns = [

    #Resident
    path("<uuid:lead_uuid>/complete-onboarding/",CompleteOnboardingAPIView.as_view(), name="complete-onboarding"),
    path("assignments/<uuid:assignment_uuid>/move-out/", MoveOutAPIView.as_view(), name="lead-move-out"),
    path("assignments/<uuid:assignment_uuid>/re-admit/", ReAdmitAPIView.as_view(),name="lead-re-admit"),
    path("residents/", ResidentListAPIView.as_view(),name="resident-list"),


    #Lead
    path("", LeadAPIView.as_view(), name="leads"),            # POST + GET
    path("<uuid:uuid>/", LeadDetailAPIView.as_view(), name="lead"), # GET + PUT
    path("<uuid:uuid>/refresh-status/", LeadRefreshStatusAPIView.as_view(), name="lead-refresh-status"),  # POST

    path("<uuid:uuid>/reject/", RejectReferralAPIView.as_view(), name="reject-referral"),
    path("assignments/<uuid:assignment_uuid>/transfer/", TransferResidentAPIView.as_view(),name="transfer-resident",
    ),

]
