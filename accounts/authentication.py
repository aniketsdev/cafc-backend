"""
DRF authentication that reads JWT from cookies.
Required so that API views see request.user when the client sends access_token cookie
(DRF's JWTAuthentication only reads the Authorization header and overwrites request.user when it returns None).
"""
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class JWTCookieAuthentication(JWTAuthentication):
    """
    Authenticate using JWT access token from the access_token cookie.
    Use this when the frontend sends the token in a cookie (e.g. cross-origin with credentials).
    """

    def authenticate(self, request):
        raw_token = request.COOKIES.get("access_token")
        if not raw_token:
            return None
        try:
            validated_token = self.get_validated_token(raw_token)
            return (self.get_user(validated_token), validated_token)
        except (InvalidToken, TokenError):
            return None
