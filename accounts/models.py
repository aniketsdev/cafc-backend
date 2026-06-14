from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.utils import timezone
from .managers import UserManager
from django.conf import settings
import uuid



#---------------------Role Model-----------------------------------------s
class Role(models.Model):
    id = models.BigAutoField(primary_key=True)

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )

    name = models.CharField(
        max_length=255,
        unique=True   
    )

    type = models.CharField(
        max_length=50 
    )
    description = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.name
    
#--------------User Model--------------------------------------
class User(AbstractBaseUser, PermissionsMixin):
    id = models.BigAutoField(primary_key=True)
    uuid = models.CharField(
        max_length=36,
        unique=True,
        default=uuid.uuid4,
        editable=False
    )

    group_home = models.ForeignKey(
    "group_home.GroupHome",
    on_delete=models.PROTECT,
    related_name="users",
    null=True,
    blank=True
    )
    
    group_homes = models.ManyToManyField(
        "group_home.GroupHome",
        related_name="multi_users",
        blank=True
    )


    username = models.CharField(
        max_length=150,
        unique=True
    )

    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255,null=True,blank=True)

    email = models.EmailField(max_length=255,unique=True,null=True,blank=True)    
    password = models.CharField(max_length=128, null=True, blank=True)

    ssn = models.CharField(max_length=255, blank=True, null=True)
    npi = models.CharField(max_length=255, blank=True, null=True)

    phone = models.BigIntegerField(blank=True, null=True)
    
    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        related_name="users"
    )

    active = models.BooleanField(default=True)

    last_login = models.DateTimeField(blank=True, null=True)
    email_verified_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    # 🔑 REQUIRED CONFIG
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username","first_name", "last_name"]

    objects = UserManager()

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

 #------------------------AddressModel-------------------------------
 
class Address(models.Model):

    id = models.BigAutoField(primary_key=True)
    uuid = models.CharField(
        max_length=36,
        unique=True,
        default=uuid.uuid4,
        editable=False
    )

    line1 = models.CharField(max_length=255, blank=True, null=True)
    line2 = models.CharField(max_length=255, blank=True, null=True)

    city = models.CharField(max_length=255, blank=True, null=True)
    state = models.CharField(max_length=255, blank=True, null=True)

    zipcode = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"Address for user_id {self.user_id}" if self.user else f"GroupHome Address {self.id}"
    
 #-----------------------PermissionModel---------------------------------------

class Permission(models.Model):

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    name = models.CharField(max_length=255)
    module = models.CharField(max_length=255)

    sub_module = models.CharField(max_length=255, blank=True, null=True)
    key = models.CharField(max_length=255, blank=True, null=True)

    description = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.name

#------------------------PermissionRoleModel----------------------
class PermissionRoles(models.Model):

    SCOPE_ALL = "ALL"
    SCOPE_ASSIGNED_HOME = "ASSIGNED_HOME"
    SCOPE_CHOICES = [
        (SCOPE_ALL, "All group homes"),
        (SCOPE_ASSIGNED_HOME, "Assigned group home(s) only"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    role = models.ForeignKey(
        'Role',
        on_delete=models.CASCADE,
        related_name='permission_roles'
    )

    permission = models.ForeignKey(
        'Permission',
        on_delete=models.CASCADE,
        related_name='permission_roles'
    )

    scope = models.CharField(
        max_length=20,
        choices=SCOPE_CHOICES,
        default=SCOPE_ALL,
    )
    display = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        unique_together = ('role', 'permission')

    def __str__(self):
        return f"Role {self.role_id} - Permission {self.permission_id} ({self.scope})"

#-----------------------PasswordResetToken--------------------------------------
class PasswordResetToken(models.Model):

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reset_tokens"
    )

    token = models.UUIDField(
        default=uuid.uuid4,      
        unique=True,
        editable=False
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"{self.user.email} - {self.token}"


#-----------------------PasswordOTP--------------------------------------
class PasswordOTP(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_otps"
    )

    otp_hash = models.CharField(max_length=255)
    attempts = models.PositiveIntegerField(default=0)
    is_used = models.BooleanField(default=False)

    # Lockout: set when attempts >= MAX_OTP_ATTEMPTS
    locked_until = models.DateTimeField(blank=True, null=True)

    # Resend rate-limiting: max N resends within a rolling window
    resend_count = models.PositiveIntegerField(default=0)
    resend_window_start = models.DateTimeField(blank=True, null=True)

    expires_at = models.DateTimeField()
    resend_available_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]

    def is_expired(self):
        return timezone.now() > self.expires_at

    def can_resend(self):
        return timezone.now() >= self.resend_available_at

    def is_locked_out(self):
        """True if the account is in a lockout window after too many failures."""
        if self.locked_until and timezone.now() < self.locked_until:
            return True
        return False

    def resend_limit_reached(self, max_resends=3, window_minutes=10):
        """True if max resends have been exhausted within the rolling window."""
        now = timezone.now()
        if self.resend_window_start and (now - self.resend_window_start).total_seconds() < window_minutes * 60:
            return self.resend_count >= max_resends
        return False