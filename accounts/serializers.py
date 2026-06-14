import random
from django.db import transaction
from django.contrib.auth.hashers import make_password
from django.contrib.auth.hashers import check_password
from rest_framework import serializers, status
from accounts.utils import (
    generate_random_password,
    generate_random_username,
    generate_uuid,
)
from .models import User, Role, Address, Permission, PermissionRoles
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from media.hooks import get_media_url
from media.models import Media



def generate_random_password(length=10):
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%"
    return "".join(random.choice(chars) for _ in range(length))

#---------------------RoleSerializer----------------------------------
class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = [
            "uuid",
            "name",
            "type",
        ]
    def validate_name(self, value):
        value = value.strip()

        if Role.objects.filter(
            name__iexact=value,
            deleted_at__isnull=True
        ).exists():
            raise serializers.ValidationError("Role name already exists")

        return value

#---------------------RoleWithPermissionsSerializer----------------------------------
class RoleWithPermissionsSerializer(serializers.ModelSerializer):
    """Role with its full permission list — used in admin settings view."""
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ["uuid", "name", "type", "description", "permissions"]
        read_only_fields = fields

    def get_permissions(self, obj):
        mappings = (
            PermissionRoles.objects.filter(role=obj)
            .select_related("permission")
            .order_by("permission__module", "permission__key")
        )
        return [
            {
                "module": m.permission.module,
                "key": m.permission.key,
                "name": m.permission.name,
                "scope": m.scope,
                "display": m.display, 
            }
            for m in mappings
        ]

#---------------------PermissionEntrySerializer----------------------------------
class PermissionEntrySerializer(serializers.Serializer):
    """Flat permission entry returned in auth/me: { module, key, scope }"""
    module = serializers.CharField()
    key = serializers.CharField()
    name = serializers.CharField()
    scope = serializers.CharField()

#---------------------UserSerializer----------------------------------
class UserSerializer(serializers.ModelSerializer):
    role = RoleSerializer(read_only=True)
    group_home = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    signature_url = serializers.SerializerMethodField()
    signature_alt_text = serializers.SerializerMethodField()
    isPasswordSet = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "uuid",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "ssn",
            "npi",
            "active",
            "role",
            "group_home",
            "group_homes",
            "avatar_url",
            "signature_url",
            "signature_alt_text",
            "isPasswordSet",
            "permissions",
        ]
        
    def get_permissions(self, obj):
        """
        Return flat list of permission entries for this user's role.
        Only included when context has include_permissions=True (auth/me endpoint).
        Returns: [{"module": "documents", "key": "view", "name": "View Documents", "scope": "ALL"}, ...]
        """
        include = self.context.get("include_permissions", False) if self.context else False
        if not include:
            return None

        if not hasattr(obj, "role") or not obj.role:
            return []

        mappings = (
            PermissionRoles.objects.filter(role=obj.role)
            .select_related("permission")
            .order_by("permission__module", "permission__key")
        )

        return [
            {
                "module": m.permission.module,
                "key": m.permission.key,
                "name": m.permission.name,
                "scope": m.scope,
                "display": m.display,   # ✅ ADD THIS LINE

            }
            for m in mappings
        ]

    def get_isPasswordSet(self, obj):
        # password is hashed string if set, empty/None if not
        return bool(obj.password)

    group_homes = serializers.SerializerMethodField()

    def get_group_homes(self, obj):
        if hasattr(obj, 'group_homes') and obj.group_homes.exists():
            return [
                {"uuid": str(gh.uuid), "name": gh.name}
                for gh in obj.group_homes.all()
            ]
        elif obj.group_home:
            return [{"uuid": str(obj.group_home.uuid), "name": obj.group_home.name}]
        return []

    def get_group_home(self, obj):
        if obj.group_home:
            return {
                "uuid": str(obj.group_home.uuid),
                "name": obj.group_home.name,
            }
        return None

    def get_avatar_url(self, obj):
        """Get avatar URL if exists. Uses precomputed avatar_urls from context when present (list view)."""
        try:
            # Use prefetched avatar URLs from list view to avoid N+1
            avatar_urls = self.context.get('avatar_urls') if self.context else None
            if avatar_urls is not None and obj.id in avatar_urls:
                return avatar_urls[obj.id]
            # First check if user has profile_picture_id field (direct reference)
            if hasattr(obj, 'profile_picture_id') and obj.profile_picture_id:
                return get_media_url(obj.profile_picture_id)
            # Otherwise, query via generic relationship
            content_type = ContentType.objects.get(app_label='accounts', model='user')
            
            # Try to find image with "profile" or "avatar" in alt_text first
            avatar_media = Media.objects.filter(
                content_type=content_type,
                object_id=obj.id,
                file_type='image',
                status='active',
                alt_text__icontains='profile'
            ).order_by('-uploaded_at').first()
            
            # If not found, get any image linked to this user (excluding signatures)
            if not avatar_media:
                avatar_media = Media.objects.filter(
                    content_type=content_type,
                    object_id=obj.id,
                    file_type='image',
                    status='active'
                ).exclude(
                    alt_text__icontains='signature'
                ).order_by('-uploaded_at').first()
            
            if avatar_media:
                return avatar_media.get_file_url()
            
            return None
        except (ContentType.DoesNotExist, Exception):
            return None

    def get_signature_url(self, obj):
        """Get signature image URL if user has a stored signature (via signature_media_id or Media linked with alt_text 'signature')."""
        try:
            # Prefer direct reference so it works after login/refetch
            if getattr(obj, 'signature_media_id', None):
                url = get_media_url(str(obj.signature_media_id).strip())
                if url:
                    return url
            # Fallback: find by generic relation (content_type + object_id)
            content_type = ContentType.objects.get_for_model(obj.__class__)
            signature_media = Media.objects.filter(
                content_type=content_type,
                object_id=obj.id,
                status='active',
                alt_text__icontains='signature',
            ).order_by('-uploaded_at').first()
            if signature_media:
                return signature_media.get_file_url()
            return None
        except (ContentType.DoesNotExist, Exception):
            return None

    def get_signature_alt_text(self, obj):
        """Get alt_text from the signature Media record to determine upload method."""
        try:
            if getattr(obj, 'signature_media_id', None):
                media = Media.objects.filter(id=obj.signature_media_id, status='active').first()
                if media:
                    return media.alt_text
            content_type = ContentType.objects.get_for_model(obj.__class__)
            signature_media = Media.objects.filter(
                content_type=content_type,
                object_id=obj.id,
                status='active',
                alt_text__icontains='signature',
            ).order_by('-uploaded_at').first()
            if signature_media:
                return signature_media.alt_text
            return None
        except (ContentType.DoesNotExist, Exception):
            return None

#---------------------UserUpdateSerializer----------------------------------
class UserUpdateSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(required=False)
    group_home_uuid = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "ssn",
            "npi",
            "active",
            "role_name",
            "group_home_uuid",
        ]

    def validate_email(self, value):
        if value in [None, ""]:
           return None
        value = value.strip().lower()
        # Must be unique; exclude the current instance when updating
        qs = User.objects.filter(email__iexact=value, deleted_at__isnull=True)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate_role_name(self, value):
        # RBAC: only users with users.role_assignment permission may change roles
        # (Admin, Program Director, BCBA, Program Manager — NOT Program Coordinator)
        from accounts.permissions import has_permission as _has_perm
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            if not _has_perm(request.user, "users.role_assignment"):
                raise serializers.ValidationError(
                    "You do not have permission to change user roles."
                )

        role = Role.objects.filter(
            name__iexact=value.strip(),
            deleted_at__isnull=True
        ).first()

        if not role:
            raise serializers.ValidationError("Invalid role")

        return role

    def validate_group_home_uuid(self, value):
        if not value:
            return []

        group_home_uuids = [v.strip() for v in str(value).split(",") if v.strip()]
        if not group_home_uuids:
            return []
            
        GroupHome = apps.get_model("group_home", "GroupHome")

        group_homes = list(GroupHome.objects.filter(
            uuid__in=group_home_uuids,
            deleted_at__isnull=True
        ))

        if not group_homes:
            raise serializers.ValidationError("Invalid group home(s)")

        return group_homes

    def update(self, instance, validated_data):
        role = validated_data.pop("role_name", None)
        group_homes_list = validated_data.pop("group_home_uuid", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if role:
            instance.role = role

        if group_homes_list is not None:
            if isinstance(group_homes_list, list) and len(group_homes_list) > 0:
                instance.group_home = group_homes_list[0]
                instance.save() # save first before setting m2m
                instance.group_homes.set(group_homes_list)
            else:
                instance.group_home = None
                instance.save()
                instance.group_homes.clear()
        else:
            instance.save()
            
        return instance

#---------------------AddressSerializer----------------------------------
class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        exclude = ['id']

#-----------------------RegisterSerializer-------------------------------------------------
class RegisterSerializer(serializers.Serializer):
    """Username is generated by the backend (random); do not send from client."""

    # User fields (no username - backend generates it)
    first_name = serializers.CharField(max_length=255, required=True)
    last_name = serializers.CharField(max_length=255, required=True)
    email = serializers.EmailField(required=False,allow_null=True,allow_blank=True)
    phone = serializers.IntegerField(required=False, allow_null=True)

    ssn = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    npi = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    role_name = serializers.CharField(write_only=True)

    group_home_uuid = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        write_only=True
    )

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already exists")
        return value.lower()

    def validate_phone(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError("Phone must be a valid integer")
        return value

    def validate_role_name(self, value):
        role = Role.objects.filter(
            name__iexact=value.strip(),
            deleted_at__isnull=True
        ).first()

        if not role:
            raise serializers.ValidationError("Invalid role")

        return role
    def validate_group_home_uuid(self, value):
        if not value:
            return []

        group_home_uuids = [v.strip() for v in str(value).split(",") if v.strip()]
        if not group_home_uuids:
            return []
            
        GroupHome = apps.get_model("group_home", "GroupHome")

        group_homes = list(GroupHome.objects.filter(
            uuid__in=group_home_uuids,
            deleted_at__isnull=True
        ))

        if not group_homes:
            raise serializers.ValidationError("Invalid group home")

        return group_homes



    # -------- CREATE -----------

    def create(self, validated_data):
        try:
                # Never use username from payload – always generate random
                validated_data.pop("username", None)

                role = validated_data.pop("role_name")  # Role instance
                group_homes_list = validated_data.pop("group_home_uuid", [])
                primary_group_home = group_homes_list[0] if group_homes_list else None

                # Always backend-generated unique random username
                username = generate_random_username()
                while User.objects.filter(username=username).exists():
                    username = generate_random_username()

                # Create User
                user = User.objects.create(
                            uuid=generate_uuid(),
                            username=username,
                            first_name=validated_data['first_name'],
                            last_name=validated_data['last_name'],
                            email=validated_data['email'],
                            phone=validated_data.get('phone'),
                            ssn=validated_data.get('ssn'),
                            npi=validated_data.get('npi'),
                            role=role,
                            group_home=primary_group_home,
                            active=True
                        )
                
                if group_homes_list:
                    user.group_homes.set(group_homes_list)
                
                # Attach raw password (for email / response)
                return user

        except serializers.ValidationError as ve:
                raise ve

        except Exception as e:
                raise serializers.ValidationError(
                    {
                        "status": status.HTTP_400_BAD_REQUEST,
                        "message": "User registration failed",
                        "error": str(e)
                    }
                )

#------------------------LoginSerializer----------------------------------
class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, required=True)

    def validate_email(self, value):
        return value.lower()
    
#---------------------setPasswordSerializer-------------------------------

class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=8, required=True)

#----------------------ForgotPasswordSerializer-----------------------------
class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)

    def validate_email(self, value):
        return value.strip().lower()

#----------------------ResendOTPSerializer----------------------------------
class ResendOTPSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)

    def validate_email(self, value):
        return value.strip().lower()

#----------------------VerifyOTPSerializer--------------------------------
class VerifyOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField(min_length=6, max_length=6)

#---------------------ChangePasswordSerializer------------------------
class ChangePasswordSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate(self, attrs):
        user = self.context["user"]

        # -------- CHECK CURRENT PASSWORD --------
        if not check_password(attrs["current_password"], user.password):
            raise serializers.ValidationError(
                {"current_password": "Current password is incorrect"}
            )

        # -------- NEW != OLD PASSWORD --------
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "New password must be different from current password"}
            )

        return attrs
#--------------------------ResendInviteSerializer------------------------------------------
class ResendInviteSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
