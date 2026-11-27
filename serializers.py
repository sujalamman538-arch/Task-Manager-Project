import re

from rest_framework import serializers

from .models import Task

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RECURRENCE_CHOICES = ["daily", "weekly", "monthly", "yearly"]


class StrictCharField(serializers.CharField):
    """Strings only. DRF would quietly turn 123 into "123"."""

    default_error_messages = {"invalid": "must be a string"}

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    """JSON true/false only. DRF would also accept "yes", 1, "on", ..."""

    default_error_messages = {"invalid": "must be a boolean (true or false)"}

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail("invalid")
        return data


class StrictDateField(serializers.DateField):
    """YYYY-MM-DD only. strptime alone would accept "2026-2-3"."""

    default_error_messages = {"invalid": "must be a valid date in YYYY-MM-DD format"}

    def to_internal_value(self, value):
        if not isinstance(value, str) or not DATE_RE.match(value):
            self.fail("invalid")
        return super().to_internal_value(value)


class TaskSerializer(serializers.ModelSerializer):
    title = StrictCharField(max_length=255)
    description = StrictCharField()
    completed = StrictBooleanField(required=False, default=False)
    due_date = StrictDateField(
        required=False, allow_null=True, input_formats=["%Y-%m-%d"]
    )
    priority = serializers.ChoiceField(
        choices=Task.Priority.choices,
        required=False,
        default=Task.Priority.MEDIUM,
        error_messages={"invalid_choice": "must be one of: low, medium, high"},
    )
    recurrence = serializers.ChoiceField(
        choices=RECURRENCE_CHOICES,
        required=False,
        allow_blank=True,
        allow_null=True,
        error_messages={
            "invalid_choice": "must be one of: daily, weekly, monthly, yearly"
        },
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "description",
            "completed",
            "due_date",
            "priority",
            "recurrence",
            "parent",
            "created_at",
            "updated_at",
        ]
        # parent is set only by the subtask and breakdown endpoints.
        read_only_fields = ["id", "parent", "created_at", "updated_at"]

    def validate_recurrence(self, value):
        return value or ""


class FromTextSerializer(serializers.Serializer):
    text = StrictCharField()
