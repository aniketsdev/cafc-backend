"""
Commit: residents/serializers.py
--------------------------------
Purpose:
- Serializers for Care Plan domain (ADLs & Goals)
- Responsible ONLY for:
  - Data representation
  - Validation
  - Safe create/update logic
- No request, permissions, or business flow logic
"""

from django.db import transaction
from rest_framework import serializers
from calendar import monthrange
from datetime import date, timedelta
from django.utils import timezone

from .models import (
    CarePlanItem,
    AssignmentCarePlanItemShift,
    CarePlanDailyLog,
    CarePlanReport,
    ResidentScheduledForm,
)
from accounts.models import User
from collections import defaultdict



# ==========================================================
# Commit 1: Shift Assignment Serializer
# ==========================================================

class AssignmentCarePlanItemShiftSerializer(serializers.ModelSerializer):

    class Meta:
        model = AssignmentCarePlanItemShift
        fields = ("shift",)


# ==========================================================
# Commit 2: Care Plan Item Serializer (ADL / GOAL)
# ==========================================================

class CarePlanItemSerializer(serializers.ModelSerializer):

    uuid = serializers.UUIDField(read_only=True)

    # Used only during create/update
    shifts = AssignmentCarePlanItemShiftSerializer(
        many=True,
        write_only=True,
        required=False
    )

    # Used only for read
    assigned_shifts = serializers.SerializerMethodField(read_only=True)

    is_archived = serializers.SerializerMethodField(read_only=True)

    resident = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field='uuid'
    )
    created_by = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field='uuid',
        required=False,
        allow_null=True
    )

    class Meta:
        model = CarePlanItem
        fields = (
            "uuid",
            "resident",
            "type",
            "title",
            "description",
            "created_by",
            "shifts",
            "assigned_shifts",
            "is_archived",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def get_assigned_shifts(self, obj):
        return list(
            obj.assigned_shifts.values_list("shift", flat=True)
        )

    def get_is_archived(self, obj):
        return obj.deleted_at is not None

    def validate(self, attrs):
        shifts = attrs.get("shifts")

        if self.instance is None and not shifts:
            raise serializers.ValidationError(
                {"shifts": "At least one shift must be assigned."}
            )

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        shifts_data = validated_data.pop("shifts", [])
        care_plan_item = CarePlanItem.objects.create(**validated_data)
        self._save_shifts(care_plan_item, shifts_data)
        return care_plan_item

    @transaction.atomic
    def update(self, instance, validated_data):
        shifts_data = validated_data.pop("shifts", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if shifts_data is not None:
            instance.assigned_shifts.all().delete()
            self._save_shifts(instance, shifts_data)

        return instance

    def _save_shifts(self, care_plan_item, shifts_data):
        seen_shifts = set()

        for shift_data in shifts_data:
            shift = shift_data.get("shift")

            if shift in seen_shifts:
                raise serializers.ValidationError(
                    {"shifts": f"Duplicate shift '{shift}' is not allowed."}
                )

            seen_shifts.add(shift)

            AssignmentCarePlanItemShift.objects.create(
                care_plan_item=care_plan_item,
                shift=shift
            )


# ==========================================================
# Commit 3: Care Plan Daily Log Serializer
# ==========================================================

class CarePlanDailyLogSerializer(serializers.ModelSerializer):
    """
    Serializer for ADL / Daily Tracking Goal logs
    POST = UPSERT
    Uniqueness:
    (care_plan_item + resident + log_date + shift)
    """

    uuid = serializers.UUIDField(read_only=True)

    care_plan_item_uuid = serializers.SlugRelatedField(
        queryset=CarePlanItem.objects.all(),
        slug_field='uuid',
        source="care_plan_item"
    )

    resident_uuid = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field='uuid',
        source="resident"
    )

    class Meta:
        model = CarePlanDailyLog
        fields = (
            "uuid",
            "care_plan_item_uuid",
            "resident_uuid",
            "log_date",
            "shift",
            "status",
            "note",
            "created_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "uuid",
            "created_at",
            "updated_at",
        )

    # ----------------------------------------------------
    # VALIDATION
    # ----------------------------------------------------
    def validate(self, attrs):
        care_plan_item = attrs.get("care_plan_item")
        resident = attrs.get("resident")
        shift = attrs.get("shift")

        # Prevent logging for archived ADL / Goal
        if care_plan_item.deleted_at is not None:
            raise serializers.ValidationError(
                "Cannot create or update logs for an archived care plan item."
            )

        # Ensure ADL belongs to resident
        if care_plan_item.resident_id != resident.id:
            raise serializers.ValidationError(
                "Resident does not match care plan item."
            )

        # Ensure shift is assigned to ADL / Goal
        assigned_shifts = care_plan_item.assigned_shifts.values_list(
            "shift", flat=True
        )
        if shift not in assigned_shifts:
            raise serializers.ValidationError(
                f"Shift '{shift}' is not assigned to this care plan item."
            )

        return attrs

    # ----------------------------------------------------
    # CREATE = UPSERT
    # ----------------------------------------------------
    
    def create(self, validated_data):
        """
        Create a new Daily Log.
        If the serializer has an instance, DRF will handle update automatically.
        """
        # Get created_by from context
        created_by = self.context.get("created_by") or self.context["request"].user
        validated_data["created_by"] = created_by

        return CarePlanDailyLog.objects.create(**validated_data)




# ==========================================================
# Commit 4: Care Plan Item LIST Serializer (READ ONLY)
# ==========================================================
# - Used ONLY for GET listing
# - Supports:
#   • Normal listing
#   • Daily tracking (date + shifts)
#   • Monthly summary (month + year)
# - No write logic
# ==========================================================
class CarePlanItemListSerializer(serializers.ModelSerializer):
    assigned_shifts = serializers.SerializerMethodField()
    daily_status = serializers.SerializerMethodField()
    monthly_progress = serializers.SerializerMethodField()
    created_by_details = serializers.SerializerMethodField()
    updated_by_details = serializers.SerializerMethodField()
    is_archived = serializers.SerializerMethodField()

    class Meta:
        model = CarePlanItem
        fields = (
            "uuid",
            "type",
            "title",
            "description",
            "assigned_shifts",
            "daily_status",
            "monthly_progress",
            "created_by_details",
            "updated_by_details",
            "is_archived",
            "created_at",
            "updated_at",
            "deleted_at",
        )

    def _user_details(self, user):
        if not user:
            return None
        role = getattr(user, "role", None)
        return {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": {"name": role.name} if role else None,
        }

    def get_created_by_details(self, obj):
        return self._user_details(obj.created_by)

    def get_updated_by_details(self, obj):
        return self._user_details(obj.updated_by)

    def get_is_archived(self, obj):
        return obj.deleted_at is not None

    # ----------------------------------------------------
    # Assigned Shifts (from assignment_care_plan_item_shifts)
    # ----------------------------------------------------
    def get_assigned_shifts(self, obj):
        return list(
            obj.assigned_shifts.values_list("shift", flat=True)
        )

    # ----------------------------------------------------
    # Daily Status (date + shift filter)
    # ----------------------------------------------------
    def get_daily_status(self, obj):
        request = self.context.get("request")
        if not request:
            return None

        date_param = request.query_params.get("date")
        shifts_param = request.query_params.get("shifts")

        if not date_param:
            return None

        shifts = (
            shifts_param.split(",")
            if shifts_param
            else self.get_assigned_shifts(obj)
        )

        # 🔥 MUST come from Prefetch(to_attr="filtered_daily_logs")
        logs = getattr(obj, "filtered_daily_logs", [])

        log_map = {log.shift: log for log in logs}

        result = {}
        for shift in shifts:
            log = log_map.get(shift)
            result[shift] = {
                "status": log.status if log else None,
                "note": log.note if log else None,
                "log_date": log.log_date if log else None,
                "created_at": log.created_at if log else None,
                "updated_at": log.updated_at if log else None,
            }

        return result

    # ----------------------------------------------------
    # Monthly Progress (month + year filter)
    # ----------------------------------------------------
    def get_monthly_progress(self, obj):
        request = self.context.get("request")
        if not request:
            return None

        month = request.query_params.get("month")
        year = request.query_params.get("year")

        if not month or not year:
            return None

        try:
            month = int(month)
            year = int(year)
        except ValueError:
            return None

        start_date = date(year, month, 1)
        end_date = date(year, month, monthrange(year, month)[1])

        logs = getattr(obj, "filtered_daily_logs", [])

        log_map = defaultdict(dict)
        for log in logs:
            log_map[log.log_date][log.shift] = log.status

        shifts = self.get_assigned_shifts(obj)

        progress = {}
        current = start_date
        while current <= end_date:
            day_result = {}

            for shift in shifts:
                status = log_map.get(current, {}).get(shift)

                if status == "WORKED" or status == "COMPLETED":
                    day_result[shift] = "WORKED"
                elif status == "DID_NOT_WORK":
                    day_result[shift] = "DID NOT WORKED"
                elif status == "COULD_NOT_WORK":
                    day_result[shift] = "COULD NOT WORKED"
                else:
                    day_result[shift] = None

            progress[str(current)] = day_result
            current += timedelta(days=1)

        return progress
  



# ==========================================================
# Commit 5: Care Plan Daily Log HISTORY Serializer (READ ONLY)
# ==========================================================
# - Used ONLY for History view
# - Shows past → present records
# - No create/update logic
# ==========================================================

# class CarePlanDailyLogHistorySerializer(serializers.ModelSerializer):

#     created_by_name = serializers.CharField(
#         source="created_by.full_name",
#         read_only=True
#     )

#     class Meta:
#         model = CarePlanDailyLog
#         fields = (
#             "uuid",
#             "log_date",
#             "shift",
#             "status",
#             "note",
#             "created_by_name",
#             "created_at",
#             "updated_at",
#         )
class CarePlanDailyLogHistorySerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CarePlanDailyLog
        fields = (
            "uuid",
            "log_date",
            "shift",
            "status",
            "note",
            "created_by_name",
            "created_at",
            "updated_at",
        )

    def get_created_by_name(self, obj):
        if obj.created_by:
            return getattr(obj.created_by, "full_name", f"{obj.created_by.first_name} {obj.created_by.last_name}")
        return "Unknown"


# ==========================================================
# Care Plan Monthly Report Serializer
# ==========================================================

class CarePlanReportSerializer(serializers.ModelSerializer):
    uuid = serializers.UUIDField(read_only=True)
    pdf_url = serializers.SerializerMethodField()

    resident_uuid = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field='uuid',
        source="resident"
    )

    class Meta:
        model = CarePlanReport
        fields = (
            "uuid",
            "resident_uuid",
            "report_month",
            "report_year",
            "report_data",
            "generated_by",
            "pdf_url",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "uuid",
            "generated_by",
            "pdf_url",
            "created_at",
            "updated_at",
        )

    def get_pdf_url(self, obj):
        if obj.pdf_file_id and obj.pdf_file:
            return obj.pdf_file.get_file_url()
        return None

    # ----------------------------------------------------
    # FIELD VALIDATION
    # ----------------------------------------------------
    def validate_report_month(self, value):
        if value < 1 or value > 12:
            raise serializers.ValidationError(
                "report_month must be between 1 and 12."
            )
        return value

    def validate_report_year(self, value):
        if value < 2000:
            raise serializers.ValidationError(
                "report_year must be greater than or equal to 2000."
            )
        return value

    # ----------------------------------------------------
    # STRUCTURE + BUSINESS VALIDATION
    # ----------------------------------------------------
    def validate(self, attrs):
        resident = attrs.get("resident")
        report_data = attrs.get("report_data")
        month = attrs.get("report_month")
        year = attrs.get("report_year")

        if not isinstance(report_data, dict):
            raise serializers.ValidationError(
                {"report_data": "Must be a JSON object."}
            )

        goals = report_data.get("goals")
        if not isinstance(goals, list) or not goals:
            raise serializers.ValidationError(
                {"report_data": "goals must be a non-empty list."}
            )

        goal_ids = []

        for index, goal in enumerate(goals):
            for field in (
                "title",
                "description",
                "report_text",
            ):
                if field not in goal:
                    raise serializers.ValidationError(
                        {
                            "report_data": (
                                f"Goal at index {index} is missing field '{field}'."
                            )
                        }
                    )

            cp_id = goal.get("care_plan_item_id")
            if cp_id:
                goal_ids.append(cp_id)

        # Validate optional top-level string fields
        for optional_field in ("methodology", "supportive_service_note"):
            value = report_data.get(optional_field)
            if value is not None and not isinstance(value, str):
                raise serializers.ValidationError(
                    {
                        "report_data": (
                            f"'{optional_field}' must be a string."
                        )
                    }
                )

        # Validate Care Plan Items (if IDs provided)
        if goal_ids:
            valid_goals_count = CarePlanItem.objects.filter(
                id__in=goal_ids,
                resident=resident,
                type=CarePlanItem.CarePlanType.GOAL,
                deleted_at__isnull=True,
            ).count()

            if valid_goals_count != len(set(goal_ids)):
                raise serializers.ValidationError(
                    {
                        "report_data": (
                            "Invalid care_plan_item_id found. "
                            "Ensure all items are active GOALs for this resident."
                        )
                    }
                )

        return attrs

    # ----------------------------------------------------
    # CREATE — replaces existing report for same month
    # ----------------------------------------------------
    def create(self, validated_data):
        request = self.context.get("request")
        validated_data["generated_by"] = (
            request.user if request and request.user.is_authenticated else None
        )

        # Soft-delete any existing report for the same resident/month/year
        CarePlanReport.objects.filter(
            resident=validated_data["resident"],
            report_month=validated_data["report_month"],
            report_year=validated_data["report_year"],
            deleted_at__isnull=True,
        ).update(deleted_at=timezone.now())

        return CarePlanReport.objects.create(**validated_data)


# ==========================================================
# ResidentScheduledForm Serializer
# ==========================================================

class ResidentScheduledFormSerializer(serializers.ModelSerializer):
    """
    Serializer for ResidentScheduledForm.
    - user is the resident — accepted as a UUID (slug field), writable.
    """

    user = serializers.SlugRelatedField(
        queryset=User.objects.all(),
        slug_field="uuid",
    )

    class Meta:
        model = ResidentScheduledForm
        fields = (
            "id",
            "user",
            "form_name",
            "scheduled_date",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")
