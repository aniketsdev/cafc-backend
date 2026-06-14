from django.db import models
from django.conf import settings
from group_home.models import GroupHome


class AuditLog(models.Model):

    ACTION_CHOICES = (
        ("CREATE", "Create"),
        ("UPDATE", "Update"),
        ("DELETE", "Delete"),
        ("LOGIN", "Login"),
        ("LOGOUT", "Logout"),
        ("OTHER", "Other"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )

    action = models.CharField(max_length=20, choices=ACTION_CHOICES)

    # 🔑 Generic entity reference
    entity_type = models.CharField(max_length=100)
    entity_id = models.CharField(max_length=50, null=True, blank=True)

    # 🏠 Group home context
    group_home = models.ForeignKey(
        GroupHome,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )

    # 📸 Immutable snapshot of group home name at time of action
    log_group_home_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Group home name at the time this log was created"
    )

    # 🎯 Target user (the person the action was performed ON/FOR)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="target_audit_logs",
    )

    # 📸 Immutable snapshot of target user name at time of action
    target_user_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Target user name at the time this log was created"
    )

    message = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} - {self.entity_type} ({self.created_at})"
