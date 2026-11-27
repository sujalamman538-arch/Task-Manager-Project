from datetime import date

from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from . import ai
from .errors import error_response, flatten_errors, validation_error_response
from .filters import TaskFilter
from .idempotency import idempotent
from .models import Task
from .serializers import FromTextSerializer, TaskSerializer

SUBTASK_NOT_ALLOWED = "A subtask cannot have subtasks or be broken down"


class TaskViewSet(viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    filterset_class = TaskFilter
    search_fields = ["title", "description"]
    ordering_fields = ["id", "title", "due_date", "created_at", "updated_at"]
    ordering = ["id"]

    def filter_queryset(self, queryset):
        # Add id as a tie-breaker so pages do not overlap when sorting by a non-unique field.
        queryset = super().filter_queryset(queryset)
        return queryset.order_by(*queryset.query.order_by, "id")

    def get_task(self):
        pk = self.kwargs["pk"]
        try:
            return Task.objects.get(pk=pk)
        except (Task.DoesNotExist, ValueError):  # ValueError: non-numeric id
            raise NotFound(f"Task with id '{pk}' not found")

    @idempotent
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        return Response(self.get_serializer(self.get_task()).data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.get("partial", False)  # True when called from PATCH
        task = self.get_task()
        serializer = self.get_serializer(task, data=request.data, partial=partial)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        self.get_task().delete()  # subtasks are removed by the CASCADE
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"], url_path="from-text")
    @idempotent
    def from_text(self, request):
        text_serializer = FromTextSerializer(data=request.data)
        if not text_serializer.is_valid():
            return validation_error_response(text_serializer.errors)

        try:
            parsed = ai.get_parser()(text_serializer.validated_data["text"], date.today())
        except ai.LLMUnavailable as exc:
            return error_response("LLM_UNAVAILABLE", str(exc), http_status=503)
        except Exception as exc:
            return error_response(
                "PARSER_ERROR",
                f"Could not parse the task text: {exc}",
                http_status=502,
            )

        # Parser output is untrusted, so it goes through the same serializer as user input.
        serializer = self.get_serializer(data=parsed)
        if not serializer.is_valid():
            return error_response(
                "INVALID_PARSER_OUTPUT",
                "Parser returned invalid task data",
                flatten_errors(serializer.errors),
                http_status=502,
            )
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def subtasks(self, request, pk=None):
        task = self.get_task()

        if request.method == "GET":
            subtasks = task.subtasks.order_by("id")
            return Response(self.get_serializer(subtasks, many=True).data)

        if task.parent_id is not None:
            return error_response("SUBTASK_NOT_ALLOWED", SUBTASK_NOT_ALLOWED)
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save(parent=task)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    @idempotent
    def breakdown(self, request, pk=None):
        task = self.get_task()
        if task.parent_id is not None:
            return error_response("SUBTASK_NOT_ALLOWED", SUBTASK_NOT_ALLOWED)

        try:
            items = ai.break_down_task(task)
        except Exception as exc:
            return error_response(
                "BREAKDOWN_ERROR",
                f"Could not break the task down: {exc}",
                http_status=502,
            )
        if not isinstance(items, list) or not items:
            return error_response(
                "INVALID_BREAKDOWN_OUTPUT",
                "Breakdown returned no subtasks",
                http_status=502,
            )

        # Validate every item first. If one is bad, save none of them.
        valid, details = [], []
        for index, item in enumerate(items):
            item_serializer = self.get_serializer(data=item)
            if item_serializer.is_valid():
                valid.append(item_serializer)
            else:
                details += flatten_errors(item_serializer.errors, f"items[{index}]")
        if details:
            return error_response(
                "INVALID_BREAKDOWN_OUTPUT",
                "Breakdown returned invalid subtask data",
                details,
                http_status=502,
            )

        with transaction.atomic():
            created = [item.save(parent=task) for item in valid]
        return Response(
            self.get_serializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )
