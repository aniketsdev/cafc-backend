from rest_framework import serializers
from django.db import transaction
from django.contrib.contenttypes.models import ContentType

from group_home.models import (
    GroupHome,
    License,
    GroupHomeRoom,
    GroupHomeShift,
)
from accounts.models import Address
from accounts.models import User, Role
from media.models import Media
from media.serializers import MediaMinimalSerializer



# =========================
# Address Serializer
# =========================
class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = [
            "uuid",
            "line1",
            "line2",
            "city",
            "state",
            "zipcode",
            "country",
        ]
        read_only_fields = ["uuid"]


# =========================
# License Serializer
# =========================
class LicenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = License
        fields = [
            "uuid",
            "number",
            "start_date",
            "expiry_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["uuid", "created_at", "updated_at"]


# =========================
# Group Home Room Serializer
# =========================
class GroupHomeRoomSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupHomeRoom
        fields = ["uuid", "room_number", "is_active", "is_occupied"]
        read_only_fields = ["uuid"]


# =========================
# Group Home Shift Serializer
# =========================
class GroupHomeShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupHomeShift
        fields = ["uuid", "shift", "start_time", "end_time", "is_active"]
        read_only_fields = ["uuid"]


# =========================
# Group Home Serializer
# =========================
class GroupHomeSerializer(serializers.ModelSerializer):
    # 🔥 Writable nested serializers
    address = AddressSerializer(required=False)
    license = LicenseSerializer(required=False)
    shifts = GroupHomeShiftSerializer(many=True, required=False)

    # 🔒 Rooms are managed ONLY via no_of_rooms logic (read-only)
    rooms = GroupHomeRoomSerializer(many=True, read_only=True)
    # Read-only: uploaded media (certificates/documents only; avatar is in avatar_url)
    media = serializers.SerializerMethodField(read_only=True)
    # Read-only: group home avatar (first image in media_items)
    avatar_url = serializers.SerializerMethodField(read_only=True)
    # Write-only: link Media records to this group home on create/update
    certificate_media_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        write_only=True,
    )
    image_media_id = serializers.UUIDField(required=False, allow_null=True, write_only=True)

    # Email: not required, nullable (group home only)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = GroupHome
        fields = [
            "uuid",
            "name",
            "timezone",
            "phone",
            "fax",
            "email",
            "emergency_contact_number",
            "address",
            "license",
            "no_of_rooms",
            "active",
            "shifts",
            "rooms",
            "media",
            "avatar_url",
            "certificate_media_ids",
            "image_media_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["uuid", "created_at", "updated_at"]

    # =========================
    # Validation
    # =========================
    def validate_no_of_rooms(self, value):
        if value <= 0:
            raise serializers.ValidationError(
                "Number of rooms must be greater than zero"
            )
        return value

    def validate_name(self, value):
        """Ensure group home name is unique (group home only)."""
        if not value or not value.strip():
            raise serializers.ValidationError("Group home name is required.")
        qs = GroupHome.objects.filter(name__iexact=value.strip())
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "A group home with this name already exists."
            )
        return value.strip()

    def get_avatar_url(self, obj):
        """Avatar via media only (same as User/incidents): Media linked to this group home with alt_text profile."""
        try:
            media_items = getattr(obj, "media_items", None)
            if not media_items:
                return None
            # Prefer media marked as profile/avatar (alt_text), same pattern as accounts/incidents
            avatar_media = next(
                (
                    m
                    for m in media_items.all()
                    if getattr(m, "file_type", None) == "image"
                    and getattr(m, "alt_text", None)
                    and "profile" in (m.alt_text or "").lower()
                ),
                None,
            )
            if not avatar_media:
                # Fallback: no alt_text convention yet, no avatar (do not use first image = avoids JPG docs as avatar)
                return None
            return avatar_media.get_file_url() if hasattr(avatar_media, "get_file_url") else None
        except Exception:
            return None

    def get_media(self, obj):
        """Uploaded documents/certificates only (exclude avatar). Avatar is the Media with alt_text profile."""
        media_items = getattr(obj, "media_items", None)
        if not media_items:
            return []
        # Exclude the profile image (alt_text contains 'profile') so media = documents only
        avatar_id = None
        for m in media_items.all():
            if (
                getattr(m, "file_type", None) == "image"
                and getattr(m, "alt_text", None)
                and "profile" in (m.alt_text or "").lower()
            ):
                avatar_id = getattr(m, "id", None)
                break
        others = [m for m in media_items.all() if getattr(m, "id", None) != avatar_id]
        return MediaMinimalSerializer(others, many=True).data

    def _link_media_to_group_home(self, group_home, certificate_media_ids, image_media_id, avatar_removed=False):
        """Set content_type/object_id on Media; mark avatar via alt_text (same flow as User avatar in media).
        Unlinks any media currently linked to this group home that are not in the new list (so removals take effect in DB).
        When image_media_id is not provided AND avatar_removed=False, preserve existing avatar.
        When avatar_removed=True (explicit null sent), delete the existing avatar."""
        try:
            content_type = ContentType.objects.get(app_label="group_home", model="grouphome")
        except ContentType.DoesNotExist:
            return
        media_ids = list(certificate_media_ids) if certificate_media_ids else []
        if image_media_id:
            media_ids.append(image_media_id)
        elif avatar_removed:
            # Explicit removal: soft-delete all profile images linked to this group home
            Media.objects.filter(
                content_type=content_type,
                object_id=group_home.id,
                alt_text__icontains="profile",
                status="active",
            ).update(status="deleted")
        else:
            # Preserve existing avatar when only certificates are updated
            existing_avatar = Media.objects.filter(
                content_type=content_type,
                object_id=group_home.id,
                alt_text__icontains="profile",
                status="active",
            ).values_list("id", flat=True).first()
            if existing_avatar:
                media_ids.append(existing_avatar)

        # Unlink media that are currently attached to this group home but not in the new list
        Media.objects.filter(
            content_type=content_type,
            object_id=group_home.id,
        ).exclude(id__in=media_ids).update(content_type=None, object_id=None)

        if not media_ids:
            return
        Media.objects.filter(
            id__in=media_ids,
            status="active",
        ).update(content_type=content_type, object_id=group_home.id)
        # Avatar in media only (same as User): mark this Media as profile image via alt_text
        if image_media_id:
            # Clear previous avatar(s) so only one profile image
            Media.objects.filter(
                content_type=content_type,
                object_id=group_home.id,
                alt_text__icontains="profile",
            ).exclude(id=image_media_id).update(alt_text="")
            Media.objects.filter(id=image_media_id, status="active").update(
                alt_text="group_home_profile"
            )

    # =========================
    # CREATE
    # =========================
    @transaction.atomic
    def create(self, validated_data):
        address_data = validated_data.pop("address", None)
        license_data = validated_data.pop("license", None)
        shifts_data = validated_data.pop("shifts", [])
        certificate_media_ids = validated_data.pop("certificate_media_ids", None) or []
        image_media_id = validated_data.pop("image_media_id", None)

        # 1️⃣ Create Group Home
        group_home = GroupHome.objects.create(**validated_data)

        # 2️⃣ Address
        if address_data:
            address = Address.objects.create(**address_data)
            group_home.address = address

        # 3️⃣ License
        if license_data:
            license_obj = License.objects.create(**license_data)
            group_home.license = license_obj

        group_home.save()

        # 4️⃣ Auto-create rooms
        GroupHomeRoom.objects.bulk_create([
            GroupHomeRoom(
                group_home=group_home,
                room_number=str(i + 1)
            )
            for i in range(group_home.no_of_rooms)
        ])

        # 5️⃣ Create shifts
        for shift in shifts_data:
            GroupHomeShift.objects.create(
                group_home=group_home,
                **shift
            )

        # 6️⃣ Link media (certificates + profile image)
        self._link_media_to_group_home(group_home, certificate_media_ids, image_media_id)

        return group_home

    # =========================
    # UPDATE (🔥 REQUIRED FOR PATCH & PUT)
    # =========================
    @transaction.atomic
    def update(self, instance, validated_data):
        address_data = validated_data.pop("address", None)
        license_data = validated_data.pop("license", None)
        shifts_data = validated_data.pop("shifts", None)
        certificate_media_ids = validated_data.pop("certificate_media_ids", None) or []
        image_media_id = validated_data.pop("image_media_id", None)

        # Detect explicit avatar removal: image_media_id key was in initial_data with a null/empty value
        image_key_sent = "image_media_id" in self.initial_data
        avatar_removed = image_key_sent and not image_media_id

        # 1️⃣ Update basic GroupHome fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # 2️⃣ Address update / create
        if address_data:
            if instance.address:
                for attr, value in address_data.items():
                    setattr(instance.address, attr, value)
                instance.address.save()
            else:
                instance.address = Address.objects.create(**address_data)
                instance.save(update_fields=["address"])

        # 3️⃣ License update / create
        if license_data:
            if instance.license:
                for attr, value in license_data.items():
                    setattr(instance.license, attr, value)
                instance.license.save()
            else:
                instance.license = License.objects.create(**license_data)
                instance.save(update_fields=["license"])

        # 4️⃣ Shifts update (replace strategy)
        if shifts_data is not None:
            instance.shifts.all().delete()
            for shift in shifts_data:
                GroupHomeShift.objects.create(
                    group_home=instance,
                    **shift
                )

        # 5️⃣ Link media (certificates + profile image)
        self._link_media_to_group_home(instance, certificate_media_ids, image_media_id, avatar_removed=avatar_removed)

        return instance


# =========================
# Group Home Status Serializer
# =========================
class GroupHomeStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupHome
        fields = ["active"]


# =========================
# Group Home User Serializer
# =========================
class GroupHomeUserSerializer(serializers.ModelSerializer):
    # Include nested role info
    role = serializers.SerializerMethodField()

    class Meta:
        model = User  # your accounts_user model
        fields = [
            "uuid",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "active",
            "last_login",
        ]

    def get_role(self, obj):
        if obj.role:
            return {
                "name": obj.role.name,
                "type": obj.role.type
            }
        return None


# =========================
# Group Home Staff Assignment Serializer
# =========================
from group_home.models import GroupHomeStaffAssignment


class GroupHomeStaffAssignmentSerializer(serializers.ModelSerializer):
    user_uuid = serializers.CharField(write_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = GroupHomeStaffAssignment
        fields = [
            "uuid", "role_type", "status",
            "user_uuid", "user_email", "user_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["uuid", "status", "created_at", "updated_at"]

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def validate_user_uuid(self, val):
        if not User.objects.filter(uuid=val, active=True, deleted_at__isnull=True).exists():
            raise serializers.ValidationError("User not found or inactive")
        return val

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Expose the user's UUID on read so the frontend can use it as the
        # value for FK-style references (e.g. assigned_program_manager_uuid)
        # without needing a second lookup.
        data["user_uuid"] = str(instance.user.uuid) if instance.user else None
        return data
