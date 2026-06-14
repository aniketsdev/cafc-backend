from django.urls import path

from accounts.views import (
    AuthMeAPIView,
    RegisterAPIView,
    LoginAPIView,
    RefreshTokenAPIView,
    LogoutAPIView,
    ForgotPasswordOTPAPIView,
    ResendOTPAPIView,
    VerifyOTPAPIView,
    SetPasswordAPIView,
    UserListAPIView,
    UserRetrieveUpdateAPIView,
    UserStatusAPIView,
    RoleAPIView,
    RolesPermissionsAPIView,
    ResendInviteAPIView,
    ChangePasswordAPIView,
    UserCanDeleteAPIView,
)

urlpatterns = [
    # Auth
    path("auth/", AuthMeAPIView.as_view(), name="auth-me"),
    path("register/", RegisterAPIView.as_view(), name="register"),
    path("user/resend-invite",ResendInviteAPIView.as_view(),name="resend-user-invite"),    
    path("set-password", SetPasswordAPIView.as_view(), name="set-password"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("refresh/", RefreshTokenAPIView.as_view(), name="token-refresh"),
    path("logout/", LogoutAPIView.as_view(), name="logout"),
    path("user/change-password",ChangePasswordAPIView.as_view(),name="change-user-password"),

    # Forgot password (OTP flow)
    path("forgot-password/", ForgotPasswordOTPAPIView.as_view(), name="forgot-password-otp"),
    path("forgot-password/resend-otp/", ResendOTPAPIView.as_view(), name="resend-otp"),
    path("forgot-password/verify-otp/", VerifyOTPAPIView.as_view(), name="verify-otp"),

     # Users
    path("users/", UserListAPIView.as_view()),
    path("users/<uuid>/can-delete/", UserCanDeleteAPIView.as_view(), name="user-can-delete"),
    path("users/<uuid>/", UserRetrieveUpdateAPIView.as_view()),      # GET + PUT
    path("users/<uuid>/status/", UserStatusAPIView.as_view()),     # PATCH

    #Role
    path("roles/", RoleAPIView.as_view(), name="roles"),    # GET + POST
    path("roles/permissions/", RolesPermissionsAPIView.as_view(), name="roles-permissions"),  # GET — admin settings
]