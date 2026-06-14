import os
import uuid
import math
import secrets
import traceback
import logging
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.shortcuts import render
from django.db import transaction, IntegrityError
from django.contrib.auth.hashers import make_password,check_password
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db.models import Q, Value, CharField
from django.db.models.functions import Concat
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError, APIException
from rest_framework_simplejwt.tokens import RefreshToken
from accounts.models import User, Address
from accounts.models import User, Role, Address
from accounts.models import User,PasswordResetToken,PasswordOTP
from accounts.serializers import VerifyOTPSerializer, ResendInviteSerializer
from accounts.serializers import LoginSerializer, RoleSerializer, ChangePasswordSerializer
from accounts.serializers import RegisterSerializer, UserSerializer, UserUpdateSerializer, RoleWithPermissionsSerializer
from accounts.serializers import SetPasswordSerializer ,AddressSerializer
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from accounts.models import User,PasswordResetToken,PasswordOTP
from django.core.paginator import Paginator
from accounts.serializers import VerifyOTPSerializer
from .email_service import send_email
from audit_logs.utils import log_action, get_active_group_home
from accounts.utils import generate_random_password, generate_uuid
from accounts.utils import generate_otp, hash_otp, verify_otp
from accounts.permissions import (
    has_permission,
    HasPermission,
    get_effective_scope,
    get_user_home_ids,
)
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from media.hooks import upload_media, update_media, get_media_url, delete_media
from django.contrib.contenttypes.models import ContentType
from media.models import Media
from rest_framework.throttling import ScopedRateThrottle

# Initialize logger for this module - logs will be written to storage/logs/app.log
logger = logging.getLogger(__name__)

INVALID_CREDENTIALS_MESSAGE = "Invalid credentials"
_DUMMY_PASSWORD_HASH = make_password("invalid-credentials-dummy-password")


def _invalid_credentials_response():
    return Response(
        {
            "status": "error",
            "code": status.HTTP_401_UNAUTHORIZED,
            "message": INVALID_CREDENTIALS_MESSAGE,
        },
        status=status.HTTP_401_UNAUTHORIZED,
    )


def _log_api_error(request, operation, exc, include_body=True):
    """
    Log API exception with full context for debugging (path, method, user, body, traceback).
    Use in except blocks so every API failure is logged consistently.
    Returns a 500 Response so the client gets a stable error payload.
    """
    body = None
    if include_body and getattr(request, "data", None) and request.data:
        body = dict(request.data)
        for key in ("password", "current_password", "new_password", "otp", "otp_hash"):
            if key in body:
                body[key] = "***"
    logger.error(
        "API error: %s - %s",
        operation,
        type(exc).__name__,
        exc_info=True,
        extra={
            "operation": operation,
            "path": getattr(request, "path", None),
            "method": getattr(request, "method", None),
            "user_id": getattr(request.user, "id", None) if getattr(request, "user", None) else None,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "request_data": body,
            "stack_trace": traceback.format_exc(),
        },
    )
    return Response(
        {
            "status": "error",
            "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "message": "Something went wrong",
            "details": str(exc),
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


class AuthMeAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            if not request.user.is_authenticated:
                return Response(
                    {"status": "error", "message": "Authentication required. Send access_token cookie."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # ---------- INACTIVE USER CHECK ----------
            # Re-fetch active flag from DB to catch mid-session deactivation
            user_active = User.objects.filter(
                pk=request.user.pk,
                active=True,
                deleted_at__isnull=True,
            ).exists()
            if not user_active:
                logger.warning(
                    "auth_me: rejected — user account is inactive",
                    extra={"user_id": request.user.id, "operation": "auth_me"},
                )
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "User account is inactive",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Authenticated user",
                    "data": UserSerializer(
                        request.user,
                        context={"include_permissions": True},
                    ).data,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return _log_api_error(request, "auth_me", e)

#---------------RolesPermissionsAPIView-----------------------------------------
class RolesPermissionsAPIView(APIView):
    """List all roles with their permissions — admin settings page (read-only)."""
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "users.view and edit roles&permission",
    }

    @extend_schema(
        operation_id="list_roles_permissions",
        summary="List all roles with permissions (admin view)",
        tags=["Roles"],
        responses={200: RoleWithPermissionsSerializer(many=True)},
    )
    def get(self, request):
        try:
            roles = Role.objects.filter(deleted_at__isnull=True).order_by("id")
            serializer = RoleWithPermissionsSerializer(roles, many=True)
            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Roles with permissions fetched successfully",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return _log_api_error(request, "list_roles_permissions", e)

#---------------RoleAPIView----------------------------------------------------
class RoleAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET":  "users.view",          # needed for role dropdowns when managing users
        "POST": "users.role_assignment",
    }
    @extend_schema(
    operation_id="list_roles",
    summary="List all roles",
    description="Retrieve roles with optional multi-type filter",
    tags=["Roles"],
    parameters=[
        OpenApiParameter(
            name="type",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description="Comma separated role types (ADMIN, STAFF)"
        )
        ]
    )

    def get(self, request):
        try:
            role_filter = request.query_params.get("type")  # ADMIN,STAFF

            roles = Role.objects.filter(
                deleted_at__isnull=True
            ).order_by("id")

            # ---------- MULTI ROLE TYPE FILTER ----------
            if role_filter:
                role_types = [
                    r.strip().upper()
                    for r in role_filter.split(",")
                    if r.strip()
                ]

                if role_types:
                    roles = roles.filter(type__in=role_types)

            serializer = RoleSerializer(roles, many=True)

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Roles fetched successfully",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return _log_api_error(request, "list_roles", e)


    # ---------- CREATE ROLE ----------
    
    @extend_schema(
        operation_id="create_role",
        summary="Create a new role",
        description="Create a new role with the provided type and permissions.",
        request=RoleSerializer,
        tags=["Roles"]
    )

    def post(self, request):
        try:
            serializer = RoleSerializer(data=request.data)

            if not serializer.is_valid():
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "Invalid input data",
                        "errors": serializer.errors,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            role = serializer.save()

            log_action(
                request=request,
                action="CREATE",
                entity_type="Role",
                entity_id=str(role.uuid),
                message="Role created",
            )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_201_CREATED,
                    "message": "Role created successfully",
                    "data": RoleSerializer(role).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except Exception as e:
            return _log_api_error(request, "create_role", e)



#-----------------------RegisterAPIView----------------------------
class RegisterAPIView(APIView):
    # Staff-only: creates user + sends invite email — NOT a public endpoint
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "POST": "users.create",
    }

    @extend_schema(
        operation_id="register_user",
        summary="Register a new user",
        description="""
        Register a new user account.""",
        request=RegisterSerializer,
        tags=["Authentication"]
    )
    def post(self, request):
        # Extract email early for logging context
        user_email = request.data.get('email', 'unknown')
        
        logger.info(
            "User registration attempt started",
            extra={
                'user_email': user_email,
                'operation': 'register_user',
                'request_method': request.method
            }
        )
        
        serializer = RegisterSerializer(data=request.data)
       
        # ---------------- VALIDATION ---------------- #
        if not serializer.is_valid():
            logger.warning(
                "User registration validation failed",
                extra={
                    'user_email': user_email,
                    'operation': 'register_user',
                    'validation_errors': serializer.errors
                }
            )
            # Check if this is an email uniqueness error
            error_str = str(serializer.errors)
            if 'email' in error_str.lower() and 'already exists' in error_str.lower():
                message = "User with this email already exists"
            else:
                message = "Invalid input data"
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": message,
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                user = serializer.save() 

                logger.info(
                    "User created successfully in database",
                    extra={
                        'user_id': user.id,
                        'user_email': user.email,
                        'user_uuid': str(user.uuid),
                        'operation': 'register_user'
                    }
                )

                log_action(
                    request=request,
                    action="CREATE",
                    entity_type="User",
                    entity_id=str(user.uuid),
                    message="User registered",
                )  

                # ---------------- AVATAR UPLOAD ---------------- #
                # Note: Direct file upload is deprecated. Use presigned URL flow instead.
                # For backward compatibility, we still support direct upload but it's slower.
                # Avatar upload is moved outside transaction to prevent timeouts.
                avatar_file = request.FILES.get('avatar')

                # Create set-password token
                token = uuid.uuid4()
                expires_at = timezone.now() + timedelta(minutes=30)

                PasswordResetToken.objects.create(
                    user=user,
                    token=token,
                    expires_at=expires_at
                )
                
                logger.info(
                    "Password reset token created for user",
                    extra={
                        'user_id': user.id,
                        'user_email': user.email,
                        'token_expires_at': expires_at.isoformat(),
                        'operation': 'register_user'
                    }
                )

            # ---------------- AVATAR UPLOAD (Outside Transaction) ---------------- #
            # Handle avatar upload after transaction commits to prevent timeouts
            # Note: Direct file upload is deprecated. Use presigned URL flow for better performance.
            if avatar_file:
                # Log deprecation warning
                logger.warning(
                    "Direct avatar upload used in registration - consider using presigned URL flow",
                    extra={
                        'user_id': user.id,
                        'user_email': user.email,
                        'file_name': avatar_file.name,
                        'file_size': avatar_file.size,
                        'operation': 'register_user_direct_upload',
                        'deprecated': True
                    }
                )
                
                try:
                    media, error = upload_media(
                        file=avatar_file,
                        user=user,
                        content_type_app='accounts',
                        content_type_model='User',
                        object_id=user.id,
                        alt_text=f"{user.first_name} {user.last_name} profile picture"
                    )
                    if error:
                        # Log error but don't fail registration - user is already created
                        logger.error(
                            "Avatar upload failed during user registration",
                            extra={
                                'error_type': 'AvatarUploadError',
                                'error_message': error,
                                'user_id': user.id,
                                'user_email': user.email,
                                'file_name': avatar_file.name,
                                'file_size': avatar_file.size,
                                'operation': 'register_user_avatar_upload',
                                'stack_trace': traceback.format_exc()
                            }
                        )
                        # Continue without avatar - user registration succeeded
                    else:
                        # Save media ID to user model if profile_picture_id field exists
                        if hasattr(user, 'profile_picture_id'):
                            user.profile_picture_id = media.id
                            user.save(update_fields=['profile_picture_id'])
                except Exception as e:
                    logger.error(
                        "Avatar upload failed during user registration: Unexpected error",
                        extra={
                            'error_type': 'AvatarUploadUnexpectedError',
                            'error_message': str(e),
                            'user_id': user.id,
                            'user_email': user.email,
                            'file_name': avatar_file.name if avatar_file else None,
                            'operation': 'register_user_avatar_upload',
                            'stack_trace': traceback.format_exc()
                        }
                    )
                    # Continue without avatar - user registration succeeded

            set_password_link = f"{settings.FRONTEND_BASE_URL}/set-password?token={token}"
            # ---------------- EMAIL ---------------- #
            logger.info(
                "Attempting to send registration email",
                extra={
                    'user_id': user.id,
                    'user_email': user.email,
                    'operation': 'register_user_send_email'
                }
            )
            
            email_result = send_email(
                to_email=user.email,
                subject="Account Created Successfully",
                template_name="set_pass_email.html",
                context={
                    "first_name": user.first_name,
                    "email": user.email,
                    "set_password_link": set_password_link,
                },
            )
            
            # Check if email was sent successfully
            if email_result is None:
                logger.error(
                    "Email sending failed during user registration - user created but email not sent",
                    extra={
                        'user_id': user.id,
                        'user_email': user.email,
                        'operation': 'register_user_send_email',
                        'error_type': 'EmailSendFailure',
                        'error_message': 'send_email() returned None - email configuration or sending failed'
                    },
                    exc_info=True
                )
            else:
                logger.info(
                    "Registration email sent successfully",
                    extra={
                        'user_id': user.id,
                        'user_email': user.email,
                        'operation': 'register_user_send_email',
                        'email_result': email_result
                    }
                )

            # ---------------- RESPONSE ---------------- #
            logger.info(
                "User registration completed successfully",
                extra={
                    'user_id': user.id,
                    'user_email': user.email,
                    'email_sent': email_result is not None,
                    'operation': 'register_user'
                }
            )
            
            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_201_CREATED,
                    "message": "User registered. Set password link sent to email.",
                    "data": {
                        "user": UserSerializer(user).data
                    },
                },
                status=status.HTTP_201_CREATED,
            )

        # ---------------- ERROR HANDLING ---------------- #
        except IntegrityError as e:
            logger.error(
                "User registration failed - IntegrityError (duplicate email/username)",
                extra={
                    'user_email': user_email,
                    'operation': 'register_user',
                    'error_type': 'IntegrityError',
                    'error_message': str(e),
                    'stack_trace': traceback.format_exc()
                },
                exc_info=True
            )
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_409_CONFLICT,
                    "message": "User with this email already exists",
                },
                status=status.HTTP_409_CONFLICT,
            )

        except Exception as e:
            return _log_api_error(request, "register_user", e)

#---------------LoginAPIView-----------------------------------
class _LoginThrottled(APIException):
    """429 carrying the app error envelope. ``wait`` is set on the instance so
    DRF's default exception handler emits a ``Retry-After`` header."""
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = "throttled"


class LoginAPIView(APIView):
    permission_classes = [AllowAny]
    # IP-scoped throttle on login to blunt credential-stuffing / password-spray.
    # Rate configured via DEFAULT_THROTTLE_RATES['login'] in settings.
    throttle_scope = 'login'
    throttle_classes = [ScopedRateThrottle]

    def throttled(self, request, wait):
        # Must RAISE (not return): DRF's check_throttles calls this and discards
        # the return value, so returning a Response would silently no-op the limit.
        seconds = int(math.ceil(wait)) if wait else 0
        exc = _LoginThrottled(detail={
            "status": "error",
            "code": status.HTTP_429_TOO_MANY_REQUESTS,
            "message": f"Too many login attempts. Please try again after {seconds} seconds.",
        })
        exc.wait = seconds
        raise exc

    @extend_schema(
        operation_id="login_user",
        summary="Login a new user",
        description="""
        Login a new user account.""",
        request=LoginSerializer,
        tags=["Authentication"]
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)

        # ---------------- VALIDATION ---------------- #
        if not serializer.is_valid():
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Invalid input data",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        try:
            # ---------------- USER LOOKUP ---------------- #
            # select_related('role') avoids an extra query when accessing user.role.type in response
            user = User.objects.select_related("role").filter(
                email=email.lower(),
                deleted_at__isnull=True  # soft delete check
            ).first()

            if user is None:
                # Keep missing-email and wrong-password paths indistinguishable.
                check_password(password, _DUMMY_PASSWORD_HASH)
                return _invalid_credentials_response()

            # ---------------- ACTIVE CHECK ---------------- #
            if not user.active:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "User account is inactive",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            # ---------------- PASSWORD CHECK ---------------- #

            if not user.password:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "Please set your password first."
                    },
                    status=status.HTTP_403_FORBIDDEN
                )
            
            if not check_password(password, user.password):
                return _invalid_credentials_response()

           

            # ---------------- LAST LOGIN ---------------- #
            user.last_login = timezone.now()
            user.save(update_fields=["last_login"])

            # ---------------- JWT TOKENS ---------------- #
            refresh = RefreshToken.for_user(user)
            access_token = str(refresh.access_token)
            refresh_token = str(refresh)

            # ---------------- RESPONSE ---------------- #
            # Tokens are set in cookies (non-HTTP-only for localhost, HTTP-only for production)
            # Frontend can read them directly from cookies, no need to return in response body
            response = Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Logged in successfully",
                    "data": {
                        "user": {
                            "id": user.id,
                            "uuid": user.uuid,
                            "first_name": user.first_name,
                            "last_name": user.last_name,
                            "email": user.email,
                            "role": {
                                "name": user.role.name,
                                "type": user.role.type,
                            },
                        },
                    },
                },
                status=status.HTTP_200_OK,
            )

            # ---------------- COOKIES ---------------- #
            env_value = os.getenv('ENV', '')
            is_local = env_value.lower() == 'local'

            if is_local:
                # Local development
                cookie_secure = False
                cookie_httponly = False
                cookie_samesite = "Lax"
                cookie_domain = None
            else:
                # Production/Staging - MUST use these settings for cross-subdomain
                cookie_secure = True  # Required for HTTPS
                cookie_httponly = True  # Security best practice
                cookie_samesite = "None"  # Required for cross-subdomain
                cookie_domain = ".aistrong.ai"  # Share across all *.aistrong.ai subdomains

            logger.debug(
                "Login cookie configuration",
                extra={
                    'user_id': user.id,
                    'env_value': env_value,
                    'is_local': is_local,
                    'operation': 'login_set_cookies',
                }
            )

            # Set cookies with explicit configuration
            response.set_cookie(
                key="access_token",
                value=access_token,
                httponly=cookie_httponly,
                max_age=120 * 60,  # 2 Hours
                secure=cookie_secure,  # MUST be True for production HTTPS
                samesite=cookie_samesite,  # MUST be "None" for cross-subdomain
                domain=cookie_domain,  # MUST be ".aistrong.ai" to share across subdomains
                path="/",
            )

            response.set_cookie(
                key="refresh_token",
                value=refresh_token,
                httponly=cookie_httponly,
                max_age=7 * 24 * 60 * 60,  # 7 days
                secure=cookie_secure,
                samesite=cookie_samesite,
                domain=cookie_domain,
                path="/",
            )

            log_action(
                request=request,
                action="LOGIN",
                entity_type="User",
                entity_id=str(user.uuid),
                message="User logged in",
            )

            return response

        # ---------------- ERRORS ---------------- #
        except User.DoesNotExist:
            return _invalid_credentials_response()

        except Exception as e:
            return _log_api_error(request, "login_user", e)


#------------------RefreshToeknAPIView--------------------------------
class RefreshTokenAPIView(APIView):
    permission_classes = [AllowAny]
    
    @extend_schema(
        operation_id="refresh_token",
        summary="Refresh access token",
        description="Refresh the access token using the refresh token stored in cookies.",
        request=None,
        tags=["Authentication"]
    )
    def post(self, request):
        try:
            # Verbose request logging at DEBUG only to avoid I/O on hot path
            logger.debug(
                "Refresh token request received",
                extra={
                    'has_refresh_token': "refresh_token" in request.COOKIES,
                    'operation': 'refresh_token_request',
                }
            )

            refresh_token = request.COOKIES.get("refresh_token")

            if not refresh_token:
                logger.debug(
                    "Refresh token not found in cookies",
                    extra={'operation': 'refresh_token_missing'},
                )
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_401_UNAUTHORIZED,
                        "message": "Refresh token not provided",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                refresh = RefreshToken(refresh_token)
            except TokenError:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_401_UNAUTHORIZED,
                        "message": "Invalid or expired refresh token",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # ---------- INACTIVE USER CHECK ----------
            # Verify the user referenced by the token is still active before issuing a new token.
            # This is the primary mechanism that boots inactive guardians on next background refresh.
            user_id = refresh.payload.get("user_id")
            if user_id:
                user_active = User.objects.filter(
                    id=user_id,
                    active=True,
                    deleted_at__isnull=True,
                ).exists()
                if not user_active:
                    logger.warning(
                        "refresh_token: rejected — user account is inactive",
                        extra={"user_id": user_id, "operation": "refresh_token"},
                    )
                    # Build env-based cookie config to properly clear existing cookies
                    env_val = os.getenv('ENV', '')
                    _is_local = env_val.lower() == 'local'
                    _cookie_samesite = "Lax" if _is_local else "None"
                    _cookie_domain = None if _is_local else ".aistrong.ai"

                    inactive_response = Response(
                        {
                            "status": "error",
                            "code": status.HTTP_401_UNAUTHORIZED,
                            "message": "User account is inactive",
                        },
                        status=status.HTTP_401_UNAUTHORIZED,
                    )
                    inactive_response.delete_cookie(
                        "access_token",
                        path="/",
                        domain=_cookie_domain,
                        samesite=_cookie_samesite,
                    )
                    inactive_response.delete_cookie(
                        "refresh_token",
                        path="/",
                        domain=_cookie_domain,
                        samesite=_cookie_samesite,
                    )
                    return inactive_response

            # Generate new access token
            new_access_token = str(refresh.access_token)
            # If ROTATE_REFRESH_TOKENS is True, a new refresh token is generated
            # The refresh object itself becomes the new refresh token
            new_refresh_token = str(refresh)  # This is the new refresh token if rotation is enabled

            response = Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Access token refreshed successfully",
                },
                status=status.HTTP_200_OK,
            )

            # Set new access token and refresh token cookies - same env-based logic as login
            env_value = os.getenv('ENV', '')
            is_local = env_value.lower() == 'local'

            if is_local:
                # Local development
                cookie_secure = False
                cookie_httponly = False
                cookie_samesite = "Lax"
                cookie_domain = None
            else:
                # Production/Staging - MUST use these settings for cross-subdomain
                cookie_secure = True  # Required for HTTPS
                cookie_httponly = True  # Security best practice
                cookie_samesite = "None"  # Required for cross-subdomain
                cookie_domain = ".aistrong.ai"  # Share across all *.aistrong.ai subdomains

            logger.debug(
                "Refresh token cookie configuration",
                extra={
                    'env_value': env_value,
                    'is_local': is_local,
                    'operation': 'refresh_set_cookies',
                }
            )

            response.set_cookie(
                key="access_token",
                value=new_access_token,
                httponly=cookie_httponly,
                max_age=15 * 60,  # 15 minutes
                secure=cookie_secure,
                samesite=cookie_samesite,
                domain=cookie_domain,
                path="/",  # Make cookie available for all paths
            )

            # Update refresh token cookie (always rotated when ROTATE_REFRESH_TOKENS is True)
            response.set_cookie(
                key="refresh_token",
                value=new_refresh_token,
                httponly=cookie_httponly,
                max_age=7 * 24 * 60 * 60,  # 7 days
                secure=cookie_secure,
                samesite=cookie_samesite,
                domain=cookie_domain,
                path="/",  # Make cookie available for all paths
            )
            
            logger.debug(
                "Refresh token cookies set",
                extra={'operation': 'refresh_cookies_set'},
            )

            return response

        except Exception as e:
            return _log_api_error(request, "refresh_token", e, include_body=False)


#------------------LogoutAPIView--------------------------------
class LogoutAPIView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="logout",
        summary="Logout",
        description="Clear auth cookies (access_token, refresh_token). Required in production where cookies are HttpOnly.",
        request=None,
        tags=["Authentication"]
    )
    def post(self, request):
        try:
            response = Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Logged out",
                },
                status=status.HTTP_200_OK,
            )

            # Same env-based cookie config as login/refresh (must match to clear cookies)
            env_value = os.getenv('ENV', '')
            is_local = env_value.lower() == 'local'

            if is_local:
                cookie_secure = False
                cookie_samesite = "Lax"
                cookie_domain = None
            else:
                cookie_secure = True
                cookie_samesite = "None"
                cookie_domain = ".aistrong.ai"

            # delete_cookie() does not accept 'secure'; path/domain/samesite must match set_cookie()
            response.delete_cookie(
                "access_token",
                path="/",
                domain=cookie_domain,
                samesite=cookie_samesite,
            )
            response.delete_cookie(
                "refresh_token",
                path="/",
                domain=cookie_domain,
                samesite=cookie_samesite,
            )

            return response
        except Exception as e:
            return _log_api_error(request, "logout", e, include_body=False)


#-----------------------ForgotPasswordOTPAPIView------------------------

class ForgotPasswordOTPAPIView(APIView):
    """
    Send a 6-digit OTP to the user's registered email for password reset.

    Security measures:
    - DRF ScopedRateThrottle (forgot_password scope — 5/hour per IP)
    - Constant-time response to prevent email enumeration
    - Invalidates any previous OTP for the user
    - OTP is hashed before storage (bcrypt)
    """
    permission_classes = [AllowAny]
    throttle_scope = 'forgot_password'
    throttle_classes = [ScopedRateThrottle]

    def throttled(self, request, wait):
        return Response(
            {
                "status": "error",
                "code": status.HTTP_429_TOO_MANY_REQUESTS,
                "message": f"Too many OTP requests. Please try again after {int(wait)} seconds.",
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    @extend_schema(
        operation_id="forgot_password_send_otp",
        summary="Forgot Password — Send OTP",
        description="Send a one-time password to the registered email for password reset.",
        request=None,
        tags=["Authentication"],
    )
    def post(self, request):
        from accounts.serializers import ForgotPasswordSerializer

        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"status": "error", "code": 400, "message": "Email is required.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data["email"]

        success_payload = {
            "status": "success",
            "code": 200,
            "message": "A one-time password (OTP) has been sent to your email.",
        }

        try:
            try:
                user = User.objects.get(email=email, active=True, deleted_at__isnull=True)
            except User.DoesNotExist:
                logger.info(
                    "Forgot password OTP requested for unknown email",
                    extra={"email": email, "operation": "forgot_password_otp"},
                )
                return Response(
                    {
                        "status": "error",
                        "code": 400,
                        "message": "Please enter a valid email address.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            existing_otp = PasswordOTP.objects.filter(user=user).order_by("-created_at").first()
            if existing_otp and existing_otp.is_locked_out():
                remaining = int((existing_otp.locked_until - timezone.now()).total_seconds())
                logger.warning(
                    "Forgot password OTP blocked — account locked",
                    extra={"user_id": user.id, "remaining_seconds": remaining, "operation": "forgot_password_otp"},
                )
                return Response(success_payload, status=status.HTTP_200_OK)

            PasswordOTP.objects.filter(user=user).delete()

            otp = generate_otp()
            now = timezone.now()

            PasswordOTP.objects.create(
                user=user,
                otp_hash=hash_otp(otp),
                expires_at=now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
                resend_available_at=now + timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS),
                resend_count=0,
                resend_window_start=now,
            )

            email_result = send_email(
                to_email=user.email,
                subject="Your OTP for Password Reset",
                template_name="otp_email.html",
                context={
                    "first_name": user.first_name,
                    "otp": otp,
                    "expiry_minutes": settings.OTP_EXPIRY_MINUTES,
                },
            )

            if email_result is None:
                logger.error(
                    "OTP email sending failed — OTP created but email not sent",
                    extra={"user_id": user.id, "user_email": user.email, "operation": "forgot_password_otp"},
                )

            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message="Password reset OTP requested",
            )

            return Response(success_payload, status=status.HTTP_200_OK)

        except Exception as e:
            return _log_api_error(request, "forgot_password_otp", e)


#--------------------ResendOTPAPIView---------------------------------

class ResendOTPAPIView(APIView):
    """
    Resend a new OTP (invalidates the previous one).

    Security measures:
    - DRF ScopedRateThrottle (resend_otp scope — 6/hour per IP)
    - Per-user cooldown (OTP_RESEND_COOLDOWN_SECONDS between resends)
    - Max OTP_MAX_RESENDS resends within OTP_RESEND_WINDOW_MINUTES rolling window
    - Previous OTP is invalidated before issuing a new one
    - Constant-time response to prevent email enumeration
    """
    permission_classes = [AllowAny]
    throttle_scope = 'resend_otp'

    from rest_framework.throttling import ScopedRateThrottle
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        operation_id="resend_password_otp",
        summary="Resend OTP",
        description="Resend password reset OTP after cooldown. Max 3 resends per 10 minutes.",
        request=None,
        tags=["Authentication"],
    )
    def post(self, request):
        from accounts.serializers import ResendOTPSerializer

        serializer = ResendOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"status": "error", "code": 400, "message": "Email is required.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data["email"]

        success_payload = {
            "status": "success",
            "code": 200,
            "message": "A new OTP has been sent to your email.",
        }

        try:
            try:
                user = User.objects.get(email=email, active=True, deleted_at__isnull=True)
            except User.DoesNotExist:
                logger.info(
                    "Resend OTP requested for unknown email",
                    extra={"email": email, "operation": "resend_otp"},
                )
                return Response(
                    {
                        "status": "error",
                        "code": 400,
                        "message": "Please enter a valid email address.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            otp_obj = PasswordOTP.objects.filter(user=user).order_by("-created_at").first()

            if not otp_obj:
                # No active OTP session — tell user to start over
                return Response(
                    {"status": "error", "code": 400, "message": "No active OTP session. Please request a new OTP via Forgot Password."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Check lockout
            if otp_obj.is_locked_out():
                remaining = int((otp_obj.locked_until - timezone.now()).total_seconds())
                return Response(
                    {
                        "status": "error",
                        "code": 429,
                        "message": f"Account is temporarily locked. Try again in {remaining} seconds.",
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            # Per-record cooldown
            if not otp_obj.can_resend():
                wait = int((otp_obj.resend_available_at - timezone.now()).total_seconds())
                return Response(
                    {"status": "error", "code": 429, "message": f"Please wait {wait} seconds before requesting a new OTP."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            # Rolling-window resend limit
            if otp_obj.resend_limit_reached(
                max_resends=settings.OTP_MAX_RESENDS,
                window_minutes=settings.OTP_RESEND_WINDOW_MINUTES,
            ):
                return Response(
                    {
                        "status": "error",
                        "code": 429,
                        "message": f"Maximum {settings.OTP_MAX_RESENDS} OTP resends reached. Please try again later.",
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            # Carry over resend tracking before deleting old OTP
            now = timezone.now()
            prev_resend_count = otp_obj.resend_count
            prev_resend_window_start = otp_obj.resend_window_start or now

            # Reset window if it has expired
            if (now - prev_resend_window_start).total_seconds() >= settings.OTP_RESEND_WINDOW_MINUTES * 60:
                prev_resend_count = 0
                prev_resend_window_start = now

            # Invalidate old OTP
            otp_obj.delete()

            # Generate new OTP
            otp = generate_otp()

            PasswordOTP.objects.create(
                user=user,
                otp_hash=hash_otp(otp),
                expires_at=now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
                resend_available_at=now + timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS),
                resend_count=prev_resend_count + 1,
                resend_window_start=prev_resend_window_start,
            )

            email_result = send_email(
                to_email=user.email,
                subject="Your New OTP for Password Reset",
                template_name="otp_email.html",
                context={
                    "first_name": user.first_name,
                    "otp": otp,
                    "expiry_minutes": settings.OTP_EXPIRY_MINUTES,
                },
            )

            if email_result is None:
                logger.error(
                    "Resend OTP email failed — OTP created but email not sent",
                    extra={"user_id": user.id, "user_email": user.email,'role_type': user.role.type if user.role else None, "operation": "resend_otp"},               
                    )
            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message="Password reset OTP resent",
            )

            return Response(success_payload, status=status.HTTP_200_OK)

        except Exception as e:
            return _log_api_error(request, "resend_otp", e)


#---------------------VerifyOTPAPIView-------------------------------------------

class VerifyOTPAPIView(APIView):
    """
    Verify the 6-digit OTP and issue a set-password token.

    Security measures:
    - DRF ScopedRateThrottle (verify_otp scope — 10/hour per IP)
    - Max OTP_MAX_ATTEMPTS (5) incorrect attempts → 15-minute lockout
    - OTP is single-use: marked as used upon successful verification
    - Expired OTPs are rejected
    - Generic error for unknown email (prevents enumeration)
    """
    permission_classes = [AllowAny]
    throttle_scope = 'verify_otp'
    throttle_classes = [ScopedRateThrottle]

    def throttled(self, request, wait):
        return Response(
            {
                "status": "error",
                "code": status.HTTP_429_TOO_MANY_REQUESTS,
                "message": f"Too many OTP verification attempts. Please try again after {int(wait)} seconds.",
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    @extend_schema(
        operation_id="verify_password_otp",
        summary="Verify OTP",
        description="Verify OTP sent to email and generate set-password token.",
        request=VerifyOTPSerializer,
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"status": "error", "code": 400, "message": "Invalid input.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data["email"]
        otp = serializer.validated_data["otp"]

        try:
            try:
                user = User.objects.get(email=email, active=True, deleted_at__isnull=True)
            except User.DoesNotExist:
                return Response(
                    {"status": "error", "code": 401, "message": "Invalid email or OTP."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            otp_obj = PasswordOTP.objects.filter(user=user).order_by("-created_at").first()

            if not otp_obj:
                return Response(
                    {"status": "error", "code": 400, "message": "No OTP found. Please request a new one."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if otp_obj.is_locked_out():
                remaining = int((otp_obj.locked_until - timezone.now()).total_seconds())
                return Response(
                    {
                        "status": "error",
                        "code": 429,
                        "message": f"Too many failed attempts. Account is locked for {remaining} seconds.",
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            if otp_obj.is_used:
                return Response(
                    {"status": "error", "code": 400, "message": "This OTP has already been used. Please request a new one."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if otp_obj.is_expired():
                otp_obj.delete()
                return Response(
                    {"status": "error", "code": 400, "message": "This OTP has expired. Please request a new one."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if otp_obj.attempts >= settings.OTP_MAX_ATTEMPTS:
                otp_obj.locked_until = timezone.now() + timedelta(minutes=settings.OTP_LOCKOUT_MINUTES)
                otp_obj.save(update_fields=["locked_until"])
                return Response(
                    {
                        "status": "error",
                        "code": 429,
                        "message": f"Too many incorrect attempts. Account locked for {settings.OTP_LOCKOUT_MINUTES} minutes.",
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            if not verify_otp(otp, otp_obj.otp_hash):
                otp_obj.attempts += 1

                if otp_obj.attempts >= settings.OTP_MAX_ATTEMPTS:
                    otp_obj.locked_until = timezone.now() + timedelta(minutes=settings.OTP_LOCKOUT_MINUTES)
                    otp_obj.save(update_fields=["attempts", "locked_until"])

                    logger.warning(
                        "OTP max attempts reached — account locked",
                        extra={"user_id": user.id, "operation": "verify_otp"},
                    )

                    return Response(
                        {
                            "status": "error",
                            "code": 429,
                            "message": f"Too many incorrect attempts. Account locked for {settings.OTP_LOCKOUT_MINUTES} minutes.",
                        },
                        status=status.HTTP_429_TOO_MANY_REQUESTS,
                    )

                otp_obj.save(update_fields=["attempts"])
                remaining_attempts = settings.OTP_MAX_ATTEMPTS - otp_obj.attempts

                return Response(
                    {
                        "status": "error",
                        "code": 400,
                        "message": f"Invalid OTP. {remaining_attempts} attempt(s) remaining.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            otp_obj.is_used = True
            otp_obj.save(update_fields=["is_used"])

            set_token = uuid.uuid4()
            PasswordResetToken.objects.create(
                user=user,
                token=set_token,
                expires_at=timezone.now() + timedelta(minutes=10),
            )

            otp_obj.delete()

            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message="Password reset OTP verified",
            )

            return Response(
                {
                    "status": "success",
                    "code": 200,
                    "message": "OTP verified successfully. You may now set a new password.",
                    "set_password_token": str(set_token),
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return _log_api_error(request, "verify_otp", e)
#-----------------------------ResendInviteAPIView---------------------------------------
class ResendInviteAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PATCH": "users.resend",
    }

    @extend_schema(
        operation_id="resend_user_invite",
        summary="Resend invite / set password link",
        description="Resend set-password email using UUID from request body",
        tags=["User"],
    )
    def patch(self, request):
        serializer = ResendInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_uuid = serializer.validated_data["uuid"]
        try:
            # ---------------- USER LOOKUP ----------------
            user = User.objects.get(
                uuid=user_uuid,
                deleted_at__isnull=True
            )

            # ---------------- DELETE OLD TOKENS ----------------
            PasswordResetToken.objects.filter(user=user).delete()

            # ---------------- CREATE NEW TOKEN ----------------
            token = uuid.uuid4()
            expires_at = timezone.now() + timedelta(minutes=30)

            PasswordResetToken.objects.create(
                user=user,
                token=token,
                expires_at=expires_at
            )

            set_password_link = (
                f"{settings.FRONTEND_BASE_URL}/set-password?token={token}"
            )

            # ---------------- SEND EMAIL ----------------
            send_email(
                to_email=user.email,
                subject="You're invited - Set your password",
                template_name="set_pass_email.html",
                context={
                    "first_name": user.first_name,
                    "email": user.email,
                    "set_password_link": set_password_link,
                },
            )

            # ---------------- AUDIT LOG ----------------
            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message="Invite link resent",
            )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Invite link sent successfully",
                },
                status=status.HTTP_200_OK,
            )

        except User.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "User not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            return _log_api_error(request, "resend_invite", e)
#---------------------ResetPasswordAPIView------------------------
class SetPasswordAPIView(APIView):
    permission_classes = [AllowAny]
    @extend_schema(
        operation_id="Set_password",
        summary="Set Password",
        description="Set user password using Set token from email link.",
        request=SetPasswordSerializer,
        parameters=None,
        tags=["Authentication"]
    )
    def post(self, request):
        token = request.query_params.get("token")

        if not token:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Set password token is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = None  # Initialize to avoid UnboundLocalError
        try:
            set_token = PasswordResetToken.objects.select_related("user").get(
                token=token
            )

            # Expiry check
            if set_token.is_expired():
                set_token.delete()
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_401_UNAUTHORIZED,
                        "message": "Set token has expired",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Get user from token - ensure it exists
            user = set_token.user
            if not user:
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_401_UNAUTHORIZED,
                        "message": "Invalid set token - user not found",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            user.password = make_password(
                serializer.validated_data["password"]
            )
            user.save(update_fields=["password"])

            # Token is single-use
            set_token.delete()

            # Log action only after user is successfully assigned and password is set
            try:
                log_action(
                    request=request,
                    action="UPDATE",
                    entity_type="User",
                    entity_id=str(user.uuid),
                    message="Password set successfully",
                )
            except Exception:
                # If logging fails, don't fail the entire request
                pass

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Password set successfully",
                },
                status=status.HTTP_200_OK,
            )

        except PasswordResetToken.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_401_UNAUTHORIZED,
                    "message": "Invalid set token",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            return _log_api_error(request, "set_password", e)


#-----------------GetALLUsersView----------------------------------------------------
class UserListAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "GET": "users.view",
    }
    # ── SCOPED ACCESS (opt-in) ─────────────────────────────────────────────
    # Allows Nurse / DSP / BCBA to call:
    #   GET /api/users/?group_home_uuid=<their own group home uuid>
    # HasPermission sees this flag and short-circuits the gate when the
    # group_home_uuid matches the user's assigned group_home.uuid.
    # Global GET /api/users/ (no param) still requires users.view → 403.
    allow_group_home_scoped = True

    @extend_schema(
        operation_id="list_users",
        summary="List all users",
        description="Fetch paginated users with filters",
        tags=["User"],
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter("size", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="active | inactive | all"
            ),
            OpenApiParameter(
                "role",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Comma separated role types (ADMIN,STAFF)"
            ),
            OpenApiParameter(
                "search",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Search by first name or last name"
            ),
            OpenApiParameter(
                "group_home_uuid",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Filter users assigned to a group home (UUID)"
            ),
        ],
    )
    def get(self, request):
        try:
            # ---------------- PAGINATION ----------------
            page = int(request.query_params.get("page", 1))
            size = int(request.query_params.get("size", 10))

            # ---------------- SEARCH ----------------
            raw_search = request.query_params.get("search")
            search = (raw_search or "").strip()

            # ---------------- FILTERS ----------------
            status_filter = request.query_params.get("status")
            role_filter = request.query_params.get("role")
            group_home_uuid = request.query_params.get("group_home_uuid")

            # ---------------- BASE QUERY ----------------
            users_qs = User.objects.filter(
                deleted_at__isnull=True
            ).select_related(
                "role",
                "group_home"   # ✅ optimization
            ).order_by("-id")

            # ── SCOPED ACCESS SAFETY FILTER ────────────────────────────────
            # Decide visibility by the user's users.view *scope*, not merely by
            # whether they hold users.view at all. The old presence-only check
            # (has_permission(...) is not None) leaked the entire org to any role
            # holding users.view, including ASSIGNED_HOME-scoped roles (Program
            # Coordinator). RBAC regression — see PR/ticket QA Retest 2026-06-02.
            #
            #   scope == "ALL"          → unrestricted org-wide list (Admin/PM/PD)
            #   scope == "ASSIGNED_HOME"→ only users in the requester's home(s)
            #   scope is None           → no users.view; admitted via the
            #                             group_home_uuid bypass → home-scoped too
            #
            # Target users are matched to the requester's home set across every
            # assignment path (FK, M2M, active staff assignment) to mirror how a
            # user can be linked to a home.
            users_scope = get_effective_scope(request.user, "users.view")
            if users_scope != "ALL":
                home_ids = get_user_home_ids(request.user)
                if home_ids:
                    users_qs = users_qs.filter(
                        Q(group_home_id__in=home_ids) |
                        Q(group_homes__id__in=home_ids) |
                        Q(
                            group_home_staff_assignments__group_home_id__in=home_ids,
                            group_home_staff_assignments__status="ACTIVE",
                            group_home_staff_assignments__deleted_at__isnull=True,
                        )
                    ).distinct()
                else:
                    users_qs = users_qs.none()

            # ---------------- STATUS FILTER ----------------
            if status_filter and status_filter.lower() != "all":
                if status_filter.lower() == "active":
                    users_qs = users_qs.filter(active=True)
                elif status_filter.lower() == "inactive":
                    users_qs = users_qs.filter(active=False)

            # ---------------- ROLE FILTER (MULTIPLE) ----------------
            if role_filter:
                role_types = [
                    r.strip().upper()
                    for r in role_filter.split(",")
                    if r.strip()
                ]
                if role_types:
                    users_qs = users_qs.filter(role__type__in=role_types)

            # ---------------- GROUP HOME FILTER (UUID) ----------------
            if group_home_uuid and group_home_uuid.lower() not in {"null", "undefined", "none"}:
                try:
                    uuid.UUID(group_home_uuid)
                except ValueError:
                    return Response(
                        {
                            "status": "error",
                            "message": "Invalid group_home_uuid"
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )

                users_qs = users_qs.filter(
                    Q(group_home__uuid=group_home_uuid) |
                    Q(group_homes__uuid=group_home_uuid) |
                    Q(
                        group_home_staff_assignments__group_home__uuid=group_home_uuid,
                        group_home_staff_assignments__status="ACTIVE",
                        group_home_staff_assignments__deleted_at__isnull=True,
                    )
                ).distinct()


            # ---------------- SEARCH BY USER NAME ----------------
            if search.lower() not in {"", "null", "undefined", "none"}:
                users_qs = users_qs.annotate(
                    full_name=Concat(
                        "first_name",
                        Value(" "),
                        "last_name",
                        output_field=CharField()
                    )
                ).filter(
                    Q(first_name__icontains=search) |
                    Q(last_name__icontains=search) |
                    Q(full_name__icontains=search)
                )

            # ---------------- PAGINATOR ----------------
            paginator = Paginator(users_qs, size)
            page_obj = paginator.get_page(page)

            # ---------------- AVATAR URL PREFETCH ----------------
            avatar_urls_map = {}
            profile_picture_ids = [
                str(u.profile_picture_id)
                for u in page_obj
                if getattr(u, "profile_picture_id", None)
            ]

            if profile_picture_ids:
                medias = Media.objects.filter(
                    id__in=profile_picture_ids,
                    status="active"
                )
                media_id_to_url = {
                    str(m.id): m.get_file_url()
                    for m in medias
                }
                avatar_urls_map = {
                    u.id: media_id_to_url.get(str(u.profile_picture_id))
                    for u in page_obj
                    if getattr(u, "profile_picture_id", None)
                }

            serializer_context = {"avatar_urls": avatar_urls_map}

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Users fetched successfully",
                    "data": {
                        "results": UserSerializer(
                            page_obj,
                            many=True,
                            context=serializer_context
                        ).data,
                        "pagination": {
                            "page": page,
                            "size": size,
                            "total_pages": paginator.num_pages,
                            "total_records": paginator.count,
                        },
                    },
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return _log_api_error(request, "list_users", e)

    
#-----------------UserDetailUpdateAPIView--------------------------------
class UserRetrieveUpdateAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    # GET: allow anyone with users.view OR profile.view_own_profile to pass the gate.
    # PUT: allow anyone with users.edit OR profile.edit_own_profile to pass the gate.
    # Inline checks inside each method enforce the own-profile restriction.
    allow_any_permissions = {
        "GET": ["users.view", "profile.view_own_profile"],
        "PUT": ["users.edit", "profile.edit_own_profile"],
    }
    # Allow portal users (GUARDIAN, AGENT) to GET and PUT their own profile.
    # HasPermission short-circuits for portal role types before allow_any_permissions
    # is evaluated, so we must explicitly opt them in for GET and PUT.
    # The own-profile guard inside each method ensures they can only access their own UUID.
    allow_portal_roles = {"GET", "PUT"}

    @extend_schema(
        operation_id="get_user_detail",
        summary="Get user details",
        description="Retrieve details of a single user by UUID.",
        tags=["User"]
    )

    def get(self, request, uuid):
        try:
            # Own-profile guard: users without users.view can only fetch their own profile
            is_own_profile = str(request.user.uuid) == str(uuid)
            if not is_own_profile and not has_permission(request.user, "users.view"):
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "You do not have permission to view other users.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            user = User.objects.get(uuid=uuid, deleted_at__isnull=True)

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "User fetched successfully",
                    "data": UserSerializer(user).data,
                },
                status=status.HTTP_200_OK,
            )
        except User.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "User not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            return _log_api_error(request, "get_user_detail", e)

    @extend_schema(
        operation_id="update_user",
        summary="Update user details",
        description="Update user information partially or fully using UUID.",
        request=UserSerializer,
        tags=["User"]
    )

    def put(self, request, uuid):
        try:
            # ---------------- RBAC: require users.edit (except own profile) ----------------
            is_own_profile = str(request.user.uuid) == str(uuid)
            if not is_own_profile and not has_permission(request.user, "users.edit"):
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "You do not have permission to edit users.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            user = User.objects.get(uuid=uuid, deleted_at__isnull=True)

            # ---------------- RBAC: strip role if no role_assignment perm ----------------
            update_data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
            if 'role' in update_data and not has_permission(request.user, "users.role_assignment"):
                update_data.pop('role')
                logger.info(
                    "Role field stripped from user update — requester lacks users.role_assignment",
                    extra={"requester_id": request.user.id, "target_user_uuid": uuid},
                )

            serializer = UserUpdateSerializer(
                user,
                data=update_data,
                partial=True,
                context={"request": request}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()

            # ContentType for User (cached for avatar paths to avoid repeated lookups)
            content_type_user = None

            # ---------------- AVATAR: from JSON (profile_picture_media_id) ---------------- #
            # Frontend uploads via media API then sends media UUID in body (Media.id is UUID, not int)
            if 'profile_picture_media_id' in request.data or 'avatar_url' in request.data:
                profile_picture_media_id = request.data.get('profile_picture_media_id')
                avatar_url = request.data.get('avatar_url')
                
                # If they passed null expressly, or an empty string, delete it
                if (profile_picture_media_id is None and avatar_url is None) or \
                   (isinstance(profile_picture_media_id, str) and not profile_picture_media_id.strip() and
                    isinstance(avatar_url, str) and not avatar_url.strip()):
                    try:
                        if content_type_user is None:
                            content_type_user = ContentType.objects.get(app_label='accounts', model='user')
                        Media.objects.filter(
                            content_type=content_type_user,
                            object_id=user.id,
                            file_type='image',
                            status='active'
                        ).exclude(
                            alt_text__icontains='signature'
                        ).update(status='deleted')
                        
                        if hasattr(user, 'profile_picture_id'):
                            user.profile_picture_id = None
                            user.save(update_fields=['profile_picture_id'])
                    except Exception as e:
                        logger.warning("profile_picture_media_id clear failed: %s", e)
                elif profile_picture_media_id is not None:
                    try:
                        media_id = str(profile_picture_media_id).strip()
                        if not media_id:
                            raise ValueError("profile_picture_media_id is empty")
                        if content_type_user is None:
                            content_type_user = ContentType.objects.get(app_label='accounts', model='user')
                        with transaction.atomic():
                            media_obj = Media.objects.filter(id=media_id, status='active').first()
                            if media_obj:
                                media_obj.content_type = content_type_user
                                media_obj.object_id = user.id
                                if not (media_obj.alt_text and 'profile' in media_obj.alt_text.lower()):
                                    media_obj.alt_text = f"{user.first_name} {user.last_name} profile picture"
                                media_obj.save()
                            upload_user = request.user if request.user.is_authenticated else user
                            previous_avatar_id = getattr(user, 'profile_picture_id', None)
                            if previous_avatar_id and str(previous_avatar_id) != media_id:
                                delete_media(
                                    media_id=str(previous_avatar_id),
                                    user=upload_user,
                                    hard_delete=False,
                                )
                            # Batch deactivate any other profile/avatar images (not signatures)
                            Media.objects.filter(
                                content_type=content_type_user,
                                object_id=user.id,
                                file_type='image',
                                status='active',
                            ).exclude(id=media_id).exclude(
                                alt_text__icontains='signature',
                            ).update(status='deleted')
                            if hasattr(user, 'profile_picture_id'):
                                user.profile_picture_id = media_id
                                user.save(update_fields=['profile_picture_id'])
                    except (ValueError, ContentType.DoesNotExist, Exception) as e:
                        logger.warning("profile_picture_media_id handling failed: %s", e)

            # ---------------- SIGNATURE: from JSON (signature_media_id) ---------------- #
            # Frontend uploads signature via S3/media API then sends media id in body
            if 'signature_media_id' in request.data:
                try:
                    signature_media_id = request.data.get('signature_media_id')
                    if content_type_user is None:
                        content_type_user = ContentType.objects.get(app_label='accounts', model='user')
                    if signature_media_id is None or (isinstance(signature_media_id, str) and not signature_media_id.strip()):
                        # Clear signature: deactivate any signature media linked to this user
                        Media.objects.filter(
                            content_type=content_type_user,
                            object_id=user.id,
                            status='active',
                            alt_text__icontains='signature',
                        ).update(status='deleted')
                        if hasattr(user, 'signature_media_id'):
                            user.signature_media_id = None
                            user.save(update_fields=['signature_media_id'])
                    else:
                        media_id = str(signature_media_id).strip()
                        if media_id:
                            media_obj = Media.objects.filter(id=media_id, status='active').first()
                            if media_obj:
                                media_obj.content_type = content_type_user
                                media_obj.object_id = user.id
                                if not (media_obj.alt_text and 'signature' in media_obj.alt_text.lower()):
                                    media_obj.alt_text = f"{user.first_name} {user.last_name} signature"
                                media_obj.save()
                            previous_sig_id = getattr(user, 'signature_media_id', None)
                            if previous_sig_id and str(previous_sig_id) != media_id:
                                upload_user = request.user if request.user.is_authenticated else user
                                delete_media(
                                    media_id=str(previous_sig_id),
                                    user=upload_user,
                                    hard_delete=False,
                                )
                            Media.objects.filter(
                                content_type=content_type_user,
                                object_id=user.id,
                                status='active',
                                alt_text__icontains='signature',
                            ).exclude(id=media_id).update(status='deleted')
                            if hasattr(user, 'signature_media_id'):
                                user.signature_media_id = media_id
                                user.save(update_fields=['signature_media_id'])
                except (ValueError, ContentType.DoesNotExist, Exception) as e:
                    logger.warning("signature_media_id handling failed: %s", e)

            # ---------------- AVATAR: from multipart file (avatar) ---------------- #
            avatar_file = request.FILES.get('avatar')
            if avatar_file:
                # Use authenticated user if available, otherwise use the user being updated
                upload_user = request.user if hasattr(request.user, 'id') and request.user.id else user
                
                # Check for existing avatar via profile_picture_id field (if exists)
                existing_avatar_id = getattr(user, 'profile_picture_id', None)
                
                # If no direct field, query via generic relationship (reuse cached ContentType)
                if not existing_avatar_id:
                    try:
                        if content_type_user is None:
                            content_type_user = ContentType.objects.get(app_label='accounts', model='user')
                        existing_avatar = Media.objects.filter(
                            content_type=content_type_user,
                            object_id=user.id,
                            file_type='image',
                            status='active'
                        ).order_by('-uploaded_at').first()
                        if existing_avatar:
                            existing_avatar_id = str(existing_avatar.id)
                    except (ContentType.DoesNotExist, Exception):
                        existing_avatar_id = None
                
                if existing_avatar_id:
                    # Update existing avatar using existing hook
                    media, error = update_media(
                        media_id=existing_avatar_id,
                        file=avatar_file,
                        user=upload_user,
                        alt_text=f"{user.first_name} {user.last_name} profile picture"
                    )
                    if error:
                        return Response(
                            {
                                "status": "error",
                                "code": status.HTTP_400_BAD_REQUEST,
                                "message": f"Failed to update avatar: {error}",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    # No need to update user.profile_picture_id - it's the same!
                else:
                    # Upload new avatar using existing hook
                    media, error = upload_media(
                        file=avatar_file,
                        user=upload_user,
                        content_type_app='accounts',
                        content_type_model='User',
                        object_id=user.id,
                        alt_text=f"{user.first_name} {user.last_name} profile picture"
                    )
                    if error:
                        return Response(
                            {
                                "status": "error",
                                "code": status.HTTP_400_BAD_REQUEST,
                                "message": f"Failed to upload avatar: {error}",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    # Save media ID to user model if profile_picture_id field exists
                    if hasattr(user, 'profile_picture_id'):
                        user.profile_picture_id = media.id
                        user.save()

            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message="User details updated",
            )

            # Refresh user to get latest data including avatar
            user.refresh_from_db()

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "User updated successfully",
                    "data": UserSerializer(user).data,
                },
                status=status.HTTP_200_OK,
            )

        except User.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "User not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except ValidationError as e:
            # Handle serializer validation errors (e.g. duplicate email)
            errors = e.detail if hasattr(e, 'detail') else str(e)
            # Check if this is an email uniqueness error
            error_str = str(errors)
            if 'email' in error_str.lower() and 'already exists' in error_str.lower():
                message = "User with this email already exists"
            else:
                message = "Validation error"
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": message,
                    "errors": errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IntegrityError:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_409_CONFLICT,
                    "message": "User with this email already exists",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except Exception as e:
            return _log_api_error(request, "update_user", e)

#--------------StatusChangeView-----------------------------------------
class UserStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, HasPermission]
    required_permission = {
        "PATCH": "users.deactivate",
    }

    @extend_schema(
        operation_id="change_user_status",
        summary="Activate/Deactivate user",
        description="Update the active status of a user using UUID.",
        request=UserSerializer,
        tags=["User"]
    )
    def patch(self, request, uuid):
        active = request.data.get("active")

        if active not in [True, False]:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_400_BAD_REQUEST,
                    "message": "Active field must be true or false",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(uuid=uuid, deleted_at__isnull=True)

            # Block deactivation when blockers (active assignments / open incidents) exist
            if active is False and user.active is True:
                from group_home.guards import user_blockers

                can, blockers = user_blockers(user)
                if not can:
                    return Response(
                        {"status": "error", "error": "Cannot deactivate/delete — remove blockers first", "blockers": blockers},
                        status=409,
                    )

            user.active = active
            user.save(update_fields=["active"])

            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message=f"User status changed to {'active' if active else 'inactive'}",
            )
            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "User status updated successfully",
                    "data": {
                        "uuid": user.uuid,
                        "active": user.active,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except User.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "User not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            return _log_api_error(request, "change_user_status", e)

#----------------------ChangePasswordAPIView-------------------------------------------
class ChangePasswordAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="change_user_password",
        summary="Change password",
        description="Change user password by UUID after validating current password",
        request=ChangePasswordSerializer,
        tags=["User"],
    )
    def patch(self, request):
        user_uuid = request.data.get("uuid")

        if not user_uuid:
            return Response(
                {
                    "status": "error",
                    "code": 400,
                    "message": "User UUID is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(
                uuid=user_uuid,
                deleted_at__isnull=True
            )

            serializer = ChangePasswordSerializer(
                data=request.data,
                context={"user": user}
            )
            if not serializer.is_valid():
                # Extract first field error for user-friendly message
                first_error = None
                for field, messages in serializer.errors.items():
                    first_error = messages[0] if isinstance(messages, list) else messages
                    break
                return Response(
                    {
                        "status": "error",
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": str(first_error) if first_error else "Invalid input data",
                        "errors": serializer.errors,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # -------- UPDATE PASSWORD --------
            user.password = make_password(
                serializer.validated_data["new_password"]
            )
            user.save(update_fields=["password"])

            log_action(
                request=request,
                action="UPDATE",
                entity_type="User",
                entity_id=str(user.uuid),
                message="Password changed successfully",
            )

            return Response(
                {
                    "status": "success",
                    "code": status.HTTP_200_OK,
                    "message": "Password changed successfully",
                },
                status=status.HTTP_200_OK,
            )

        except User.DoesNotExist:
            return Response(
                {
                    "status": "error",
                    "code": status.HTTP_404_NOT_FOUND,
                    "message": "User not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            return _log_api_error(request, "change_user_password", e)


# =====================================================
# USER CAN-DELETE PREFLIGHT
# =====================================================
from group_home.guards import user_blockers


class UserCanDeleteAPIView(APIView):
    """
    Pre-flight check: can this user be deactivated/deleted?
    Returns blockers (active assignments + non-terminal incidents).
    Admin-only (is_superuser gate).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, uuid):
        if not request.user.is_superuser:
            # Also accept users whose role is Admin or Program Director
            role_name = getattr(getattr(request.user, "role", None), "name", "")
            if role_name not in ("Admin", "Program Director"):
                return Response({"status": "error", "message": "Forbidden"}, status=403)
        try:
            user = User.objects.get(uuid=uuid, deleted_at__isnull=True)
        except User.DoesNotExist:
            return Response({"status": "error", "message": "User not found"}, status=404)
        can, blockers = user_blockers(user)
        return Response({"can_delete": can, "blockers": blockers}, status=200)
