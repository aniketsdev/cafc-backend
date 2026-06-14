from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError, AuthenticationFailed


import os
from django.http import JsonResponse
from rest_framework import status

# Paths that do not need JWT auth (AllowAny auth endpoints). Skipping here avoids
# get_validated_token + get_user() DB hit on every login/refresh request and speeds up
# failed refresh (missing/invalid token) responses.
_JWT_SKIP_PATHS = ("/api/accounts/login/", "/api/accounts/refresh/", "/api/accounts/logout/")


class JWTAuthCookieMiddleware:
    """
    Authenticate user using JWT access token stored in HttpOnly cookies.
    Skips JWT validation for login/refresh/logout paths to improve auth API performance.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.jwt_authenticator = JWTAuthentication()

    def __call__(self, request):
        request.user = AnonymousUser()

        # Skip JWT validation for auth endpoints (no need for request.user; avoids DB + crypto work)
        path = request.path
        if path.rstrip("/") in (p.rstrip("/") for p in _JWT_SKIP_PATHS):
            return self.get_response(request)

        access_token = request.COOKIES.get("access_token")
        if not access_token:
            return self.get_response(request)

        try:
            validated_token = self.jwt_authenticator.get_validated_token(access_token)
            user = self.jwt_authenticator.get_user(validated_token)
            if user and not getattr(user, 'active', True):
                response = JsonResponse(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "User account is inactive."
                    },
                    status=status.HTTP_403_FORBIDDEN
                )
                env = os.getenv("ENV", "local")
                is_prod = env == "production"
                cookie_domain = os.getenv("COOKIE_DOMAIN")
                samesite = "None" if is_prod else "Lax"
                
                response.delete_cookie(
                    "access_token",
                    path="/",
                    domain=cookie_domain,
                    samesite=samesite
                )
                response.delete_cookie(
                    "refresh_token",
                    path="/",
                    domain=cookie_domain,
                    samesite=samesite
                )
                return response
            else:
                request.user = user
        except (InvalidToken, TokenError):
            request.user = AnonymousUser()
        except AuthenticationFailed as e:
            # This is raised by simplejwt if user.is_active is False
            if hasattr(e, 'detail') and 'inactive' in str(e.detail).lower():
                response = JsonResponse(
                    {
                        "status": "error",
                        "code": status.HTTP_403_FORBIDDEN,
                        "message": "User account is inactive."
                    },
                    status=status.HTTP_403_FORBIDDEN
                )
                env = os.getenv("ENV", "local")
                is_prod = env == "production"
                cookie_domain = os.getenv("COOKIE_DOMAIN")
                samesite = "None" if is_prod else "Lax"
                
                response.delete_cookie("access_token", path="/", domain=cookie_domain, samesite=samesite)
                response.delete_cookie("refresh_token", path="/", domain=cookie_domain, samesite=samesite)
                return response
            else:
                request.user = AnonymousUser()

        return self.get_response(request)
