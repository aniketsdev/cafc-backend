from django.db import models
import uuid
from django.contrib.contenttypes.fields import GenericRelation
from media.models import Media



# ----------------------- Time Zone Choices -----------------------
TIMEZONE_CHOICES = (
    ('Eastern Time', 'Eastern Time'),
    ('Central Time', 'Central Time'),
    ('Mountain Time', 'Mountain Time'),
    ('Pacific Time', 'Pacific Time'),
    ('Arizona Time', 'Arizona Time'),
    ('Alaska Time', 'Alaska Time'),
)

# ----------------------- Shift Choices -----------------------
SHIFT_CHOICES = (
    ("MORNING", "Morning"),
    ("EVENING", "Evening"),
    ("NIGHT", "Night"),
)

# ----------------------- License Model -----------------------
class License(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    number = models.CharField(max_length=255)
    start_date = models.DateTimeField(null=True, blank=True)
    expiry_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        db_table = "license"

    def __str__(self):
        return self.number


# ----------------------- GroupHome Model -----------------------
class GroupHome(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    name = models.CharField(max_length=255)
    timezone = models.CharField(
        max_length=50,
        choices=TIMEZONE_CHOICES,
        blank=True,
        null=True
    )
    phone = models.CharField(max_length=20)
    fax = models.CharField(max_length=20)
    email = models.EmailField()
    emergency_contact_number = models.CharField(max_length=20, blank=True, null=True)

    # 🔑 ForeignKey relationships
    address = models.ForeignKey(
        "accounts.Address",
        on_delete=models.PROTECT,
        related_name="group_homes",
        blank=True,
        null=True
    )
    license = models.ForeignKey(
        License,
        on_delete=models.PROTECT,
        related_name="group_homes",
        blank=True,
        null=True
    )

    # ✅ RENAMED FIELD
    no_of_rooms = models.BigIntegerField()

    active = models.BooleanField(default=True)


    media_items = GenericRelation(
        Media,
        content_type_field="content_type",
        object_id_field="object_id",
        related_query_name="group_home"
    )
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "group_home"

    def __str__(self):
        return self.name


# ----------------------- Group Home Rooms -----------------------
class GroupHomeRoom(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    group_home = models.ForeignKey(
        GroupHome,
        on_delete=models.CASCADE,
        related_name="rooms"
    )
    room_number = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    is_occupied = models.BooleanField(default=False)

    class Meta:
        db_table = "group_home_rooms"
        unique_together = ("group_home", "room_number")

    def __str__(self):
        return f"{self.group_home.name} - Room {self.room_number}"


# ----------------------- Group Home Shifts -----------------------
class GroupHomeShift(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    group_home = models.ForeignKey(
        GroupHome,
        on_delete=models.CASCADE,
        related_name="shifts"
    )
    shift = models.CharField(max_length=20, choices=SHIFT_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "group_home_shifts"
        unique_together = ("group_home", "shift")

    def __str__(self):
        return f"{self.group_home.name} - {self.shift}"


# ----------------------- Group Home Staff Assignment -----------------------
class GroupHomeStaffAssignment(models.Model):
    """
    Role-aware many-to-many between Users and GroupHomes.

    Replaces the legacy `User.group_home` FK as the source of truth for
    "which staff are working at which homes". Read-mostly: rows are created
    by the assignments API and soft-removed (status='INACTIVE') on unassign.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    class RoleType(models.TextChoices):
        DSP = "DSP", "DSP"
        PROGRAM_MANAGER = "PROGRAM_MANAGER", "Program Manager"
        PROGRAM_COORDINATOR = "PROGRAM_COORDINATOR", "Program Coordinator"
        BCBA = "BCBA", "BCBA"
        NURSE = "NURSE", "Nurse"
        LEAD = "LEAD", "Lead"

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    group_home = models.ForeignKey(
        GroupHome,
        on_delete=models.PROTECT,
        related_name="staff_assignments",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="group_home_staff_assignments",
    )
    role_type = models.CharField(max_length=32, choices=RoleType.choices)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "group_home_staff_assignments"
        unique_together = ("group_home", "user", "role_type")
        indexes = [
            models.Index(fields=["group_home", "role_type", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self):
        return f"{self.user_id}@{self.group_home_id} [{self.role_type}/{self.status}]"
