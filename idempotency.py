"""Idempotency-Key support for the create endpoints.

A successful response is stored under its key. Sending the same key with the same
request again returns the stored response and does not run the view again. Reusing
a key for a different request returns 422. Failed responses are not stored, so a
client can fix its input and retry with the same key.

The store is an in-memory dict: it is lost on restart and not shared between
processes.
"""

import copy
import functools
import json
import threading

from django.http import QueryDict
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from .errors import error_response

HEADER = "Idempotency-Key"

_store = {}
# Held for the whole request, so two identical requests cannot both create a row.
_lock = threading.Lock()


def clear_store():
    with _lock:
        _store.clear()


def _as_plain(data):
    return json.loads(JSONRenderer().render(data) or b"null")


def _request_body(request):
    data = request.data
    if isinstance(data, QueryDict):
        data = data.dict()
    return copy.deepcopy(data)


def idempotent(view_method):
    @functools.wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        key = (request.headers.get(HEADER) or "").strip()
        if not key:
            return view_method(self, request, *args, **kwargs)

        request_info = {"path": request.path, "body": _request_body(request)}

        with _lock:
            saved = _store.get(key)
            if saved is not None:
                if saved["request"] != request_info:
                    return error_response(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                        [{"field": HEADER, "issue": "key reused with a different request"}],
                        http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                replay = Response(copy.deepcopy(saved["data"]), status=saved["status"])
                replay["Idempotent-Replayed"] = "true"
                return replay

            response = view_method(self, request, *args, **kwargs)
            if 200 <= response.status_code < 300:
                _store[key] = {
                    "request": request_info,
                    "status": response.status_code,
                    "data": _as_plain(response.data),
                }
            return response

    return wrapper
