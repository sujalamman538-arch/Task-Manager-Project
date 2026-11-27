from datetime import date
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from . import ai, idempotency
from .models import Task

TASKS = "/api/tasks/"
FROM_TEXT = "/api/tasks/from-text/"


def valid_task(**overrides):
    data = {"title": "Buy milk", "description": "2 litres"}
    data.update(overrides)
    return data


class ApiTestCase(APITestCase):
    def setUp(self):
        idempotency.clear_store()

    def post(self, url, data=None, key=None):
        extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(url, data if data is not None else {}, format="json", **extra)

    def assertErrorShape(self, response, status, code=None):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual(list(body.keys()), ["error"])
        error = body["error"]
        self.assertEqual(set(error), {"code", "message", "details"})
        self.assertIsInstance(error["code"], str)
        self.assertIsInstance(error["message"], str)
        self.assertIsInstance(error["details"], list)
        for detail in error["details"]:
            self.assertEqual(set(detail), {"field", "issue"})
            self.assertIsInstance(detail["field"], str)
            self.assertIsInstance(detail["issue"], str)
        if code:
            self.assertEqual(error["code"], code)
        return error


class CrudTests(ApiTestCase):
    def test_create_defaults(self):
        r = self.post(TASKS, valid_task())
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["completed"])
        self.assertEqual(r.json()["priority"], "medium")
        self.assertIsNone(r.json()["due_date"])
        self.assertIsNone(r.json()["parent"])

    def test_retrieve_update_patch_delete(self):
        task_id = self.post(TASKS, valid_task()).json()["id"]
        self.assertEqual(self.client.get(f"{TASKS}{task_id}/").status_code, 200)

        r = self.client.put(
            f"{TASKS}{task_id}/",
            valid_task(title="New", completed=True, priority="high", due_date="2026-12-01"),
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "New")

        r = self.client.patch(f"{TASKS}{task_id}/", {"completed": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "New")  # untouched by PATCH
        self.assertEqual(r.json()["priority"], "high")

        self.assertEqual(self.client.delete(f"{TASKS}{task_id}/").status_code, 204)
        self.assertFalse(Task.objects.filter(pk=task_id).exists())

    def test_unknown_id_is_404_in_error_shape(self):
        for method in ("get", "put", "patch", "delete"):
            kwargs = {"format": "json"} if method in ("put", "patch") else {}
            args = (valid_task(),) if method in ("put", "patch") else ()
            r = getattr(self.client, method)(f"{TASKS}9999/", *args, **kwargs)
            self.assertErrorShape(r, 404, "NOT_FOUND")

    def test_non_numeric_id_is_404(self):
        self.assertErrorShape(self.client.get(f"{TASKS}abc/"), 404, "NOT_FOUND")

    def test_unknown_id_on_subtask_actions(self):
        for url in ("subtasks", "breakdown"):
            r = self.post(f"{TASKS}9999/{url}/", valid_task())
            self.assertErrorShape(r, 404, "NOT_FOUND")
        self.assertErrorShape(self.client.get(f"{TASKS}9999/subtasks/"), 404)

    def test_malformed_json_is_400_in_error_shape(self):
        r = self.client.post(TASKS, "{bad json", content_type="application/json")
        self.assertErrorShape(r, 400, "INVALID_JSON")

    def test_method_not_allowed_uses_error_shape(self):
        self.assertErrorShape(self.client.delete(TASKS), 405)


class ValidationTests(ApiTestCase):
    def fields(self, response):
        error = self.assertErrorShape(response, 400, "VALIDATION_ERROR")
        self.assertEqual(error["message"], "Validation failed")
        return {d["field"]: d["issue"] for d in error["details"]}

    def test_empty_and_blank_title_description(self):
        for bad in ("", "   "):
            f = self.fields(self.post(TASKS, {"title": bad, "description": bad}))
            self.assertEqual(f["title"], "must not be empty")
            self.assertEqual(f["description"], "must not be empty")

    def test_missing_fields(self):
        f = self.fields(self.post(TASKS, {}))
        self.assertEqual(f["title"], "is required")
        self.assertEqual(f["description"], "is required")

    def test_null_and_non_string_title(self):
        self.assertIn("title", self.fields(self.post(TASKS, valid_task(title=None))))
        self.assertEqual(
            self.fields(self.post(TASKS, valid_task(title=123)))["title"], "must be a string"
        )

    def test_completed_must_be_boolean(self):
        for bad in ("yes", "true", 1, 0, None, "maybe"):
            f = self.fields(self.post(TASKS, valid_task(completed=bad)))
            self.assertIn("completed", f, bad)

    def test_due_date_validation(self):
        for bad in ("2026-02-30", "2026-13-01", "01-02-2026", "2026-2-3", "tomorrow", 20260101, ""):
            f = self.fields(self.post(TASKS, valid_task(due_date=bad)))
            self.assertIn("due_date", f, bad)
        self.assertEqual(self.post(TASKS, valid_task(due_date="2028-02-29")).status_code, 201)
        self.assertEqual(self.post(TASKS, valid_task(due_date=None)).status_code, 201)

    def test_priority_choices(self):
        self.assertIn("priority", self.fields(self.post(TASKS, valid_task(priority="urgent"))))
        for ok in ("low", "medium", "high"):
            self.assertEqual(self.post(TASKS, valid_task(priority=ok)).status_code, 201)

    def test_validation_on_update(self):
        task_id = self.post(TASKS, valid_task()).json()["id"]
        r = self.client.put(f"{TASKS}{task_id}/", {"title": "", "description": "x"}, format="json")
        self.assertIn("title", self.fields(r))
        r = self.client.patch(f"{TASKS}{task_id}/", {"due_date": "2026-02-30"}, format="json")
        self.assertIn("due_date", self.fields(r))

    def test_parent_cannot_be_set_by_client(self):
        parent = self.post(TASKS, valid_task()).json()["id"]
        r = self.post(TASKS, valid_task(parent=parent))
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(r.json()["parent"])

    def test_nothing_saved_on_validation_error(self):
        self.post(TASKS, {"title": "", "description": ""})
        self.assertEqual(Task.objects.count(), 0)


class ParserTests(SimpleTestCase):
    today = date(2026, 11, 21)  # a Saturday

    def parse(self, text):
        return ai.parse_task_text(text, self.today)

    def test_rent_example(self):
        r = self.parse("Remind me to pay rent on the 1st of every month")
        self.assertEqual(r["title"], "Pay rent")
        self.assertEqual(r["due_date"], "2026-12-01")
        self.assertEqual(r["recurrence"], "monthly")
        self.assertEqual(r["description"], "Remind me to pay rent on the 1st of every month")

    def test_tomorrow_and_today(self):
        self.assertEqual(self.parse("Call mom tomorrow")["due_date"], "2026-11-22")
        self.assertEqual(self.parse("Call mom tomorrow")["title"], "Call mom")
        self.assertEqual(self.parse("Submit report today")["due_date"], "2026-11-21")
        self.assertEqual(self.parse("Call mom day after tomorrow")["due_date"], "2026-11-23")

    def test_relative_dates(self):
        self.assertEqual(self.parse("Dentist in 3 days")["due_date"], "2026-11-24")
        self.assertEqual(self.parse("Review plan in 2 weeks")["due_date"], "2026-12-05")
        self.assertEqual(self.parse("Renew visa next week")["due_date"], "2026-11-28")
        self.assertEqual(self.parse("Renew visa in 3 months")["due_date"], "2027-02-21")

    def test_weekdays_and_recurrence(self):
        r = self.parse("Team standup every monday")
        self.assertEqual((r["due_date"], r["recurrence"]), ("2026-11-23", "weekly"))
        self.assertEqual(r["title"], "Team standup")
        self.assertEqual(self.parse("Water plants every day")["recurrence"], "daily")
        self.assertEqual(self.parse("Pay insurance yearly")["recurrence"], "yearly")
        self.assertEqual(self.parse("Send invoice on friday")["due_date"], "2026-11-27")
        self.assertNotIn("recurrence", self.parse("Buy milk tomorrow"))

    def test_day_of_month_skips_months_without_that_day(self):
        self.assertEqual(self.parse("Pay bill on the 15th")["due_date"], "2026-12-15")
        self.assertEqual(self.parse("Pay bill on the 21st")["due_date"], "2026-11-21")
        p = ai.parse_task_text("Pay bill on the 31st", date(2026, 1, 31))
        self.assertEqual(p["due_date"], "2026-01-31")
        p = ai.parse_task_text("Pay bill on the 31st", date(2026, 2, 1))
        self.assertEqual(p["due_date"], "2026-03-31")

    def test_month_names_and_iso_dates(self):
        self.assertEqual(self.parse("Birthday party on March 5th")["due_date"], "2027-03-05")
        self.assertEqual(self.parse("File taxes 5 December")["due_date"], "2026-12-05")
        self.assertEqual(self.parse("Launch on 2027-01-15")["due_date"], "2027-01-15")
        # impossible ISO date is passed through so the serializer rejects it
        self.assertEqual(self.parse("Launch on 2026-02-30")["due_date"], "2026-02-30")
        with self.assertRaises(ValueError):
            self.parse("Party on February 30")

    def test_no_date(self):
        r = self.parse("Buy milk")
        self.assertEqual(r["title"], "Buy milk")
        self.assertIsNone(r["due_date"])

    def test_text_that_is_only_a_date_falls_back_to_original(self):
        self.assertEqual(self.parse("tomorrow")["title"], "Tomorrow")

    def test_empty_text_raises(self):
        for bad in ("", "   ", None, 5):
            with self.assertRaises(ValueError):
                ai.parse_task_text(bad, self.today)


class FromTextTests(ApiTestCase):
    def test_creates_task(self):
        r = self.post(FROM_TEXT, {"text": "Remind me to pay rent on the 1st of every month"})
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["title"], "Pay rent")
        self.assertEqual(body["recurrence"], "monthly")
        self.assertEqual(Task.objects.count(), 1)

    def test_invalid_input(self):
        for bad in ({}, {"text": ""}, {"text": "  "}, {"text": 5}, {"text": None}):
            r = self.post(FROM_TEXT, bad)
            self.assertErrorShape(r, 400, "VALIDATION_ERROR")
        self.assertEqual(Task.objects.count(), 0)

    def test_parser_returning_empty_title_is_502_and_saves_nothing(self):
        bad = {"title": "", "description": "x", "due_date": None}
        with mock.patch.object(ai, "parse_task_text", return_value=bad):
            r = self.post(FROM_TEXT, {"text": "anything"})
        error = self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT")
        self.assertEqual(error["details"][0], {"field": "title", "issue": "must not be empty"})
        self.assertEqual(Task.objects.count(), 0)

    def test_parser_returning_wrong_types_is_502(self):
        for bad in (
            {"title": 123, "description": "x", "due_date": None},
            {"title": "t", "description": "x", "due_date": "2026-02-30"},
            {"title": "t", "description": "x", "completed": "yes"},
            {"title": "t", "description": "x", "priority": "urgent"},
            {"title": "t", "description": "x", "recurrence": "sometimes"},
            {"description": "x"},
            ["not", "a", "dict"],
            "just a string",
        ):
            with mock.patch.object(ai, "parse_task_text", return_value=bad):
                r = self.post(FROM_TEXT, {"text": "anything"})
            self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT")
        self.assertEqual(Task.objects.count(), 0)

    def test_parser_exception_is_502(self):
        with mock.patch.object(ai, "parse_task_text", side_effect=RuntimeError("boom")):
            r = self.post(FROM_TEXT, {"text": "anything"})
        self.assertErrorShape(r, 502, "PARSER_ERROR")
        self.assertEqual(Task.objects.count(), 0)

    def test_impossible_date_in_text_is_502(self):
        r = self.post(FROM_TEXT, {"text": "Launch on 2026-02-30"})
        self.assertErrorShape(r, 502, "INVALID_PARSER_OUTPUT")
        self.assertEqual(Task.objects.count(), 0)

    @override_settings(TASK_PARSER="llm", LLM_API_KEY=None)
    def test_missing_llm_key_is_503(self):
        r = self.post(FROM_TEXT, {"text": "Buy milk"})
        self.assertErrorShape(r, 503, "LLM_UNAVAILABLE")
        self.assertEqual(Task.objects.count(), 0)


class SubtaskTests(ApiTestCase):
    def make_task(self, **kw):
        return self.post(TASKS, valid_task(**kw)).json()["id"]

    def test_create_and_list_subtasks(self):
        parent = self.make_task()
        r = self.post(f"{TASKS}{parent}/subtasks/", valid_task(title="Sub"))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["parent"], parent)

        r = self.client.get(f"{TASKS}{parent}/subtasks/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([t["title"] for t in r.json()], ["Sub"])

    def test_invalid_subtask(self):
        parent = self.make_task()
        r = self.post(f"{TASKS}{parent}/subtasks/", {"title": "", "description": ""})
        self.assertErrorShape(r, 400, "VALIDATION_ERROR")
        self.assertEqual(Task.objects.filter(parent=parent).count(), 0)

    def test_subtask_of_subtask_is_400(self):
        parent = self.make_task()
        sub = self.post(f"{TASKS}{parent}/subtasks/", valid_task()).json()["id"]
        r = self.post(f"{TASKS}{sub}/subtasks/", valid_task())
        self.assertErrorShape(r, 400, "SUBTASK_NOT_ALLOWED")
        r = self.post(f"{TASKS}{sub}/breakdown/")
        self.assertErrorShape(r, 400, "SUBTASK_NOT_ALLOWED")
        self.assertEqual(Task.objects.count(), 2)

    def test_cascade_delete(self):
        parent = self.make_task()
        self.post(f"{TASKS}{parent}/subtasks/", valid_task())
        self.post(f"{TASKS}{parent}/subtasks/", valid_task())
        other = self.make_task()
        self.assertEqual(Task.objects.count(), 4)
        self.client.delete(f"{TASKS}{parent}/")
        self.assertEqual(list(Task.objects.values_list("id", flat=True)), [other])

    def test_deleting_a_subtask_keeps_parent(self):
        parent = self.make_task()
        sub = self.post(f"{TASKS}{parent}/subtasks/", valid_task()).json()["id"]
        self.client.delete(f"{TASKS}{sub}/")
        self.assertTrue(Task.objects.filter(pk=parent).exists())

    def test_breakdown_splits_on_and_and_commas(self):
        parent = self.make_task(title="Plan trip, book flights and pack bags", due_date="2026-12-01")
        r = self.post(f"{TASKS}{parent}/breakdown/")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(
            [t["title"] for t in r.json()], ["Plan trip", "Book flights", "Pack bags"]
        )
        self.assertTrue(all(t["parent"] == parent for t in r.json()))
        self.assertTrue(all(t["due_date"] == "2026-12-01" for t in r.json()))

    def test_breakdown_template_for_single_part_title(self):
        parent = self.make_task(title="Write report")
        r = self.post(f"{TASKS}{parent}/breakdown/")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.json()), 3)

    def test_breakdown_with_one_invalid_item_saves_none(self):
        parent = self.make_task()
        items = [
            {"title": "ok", "description": "fine"},
            {"title": "", "description": "bad"},
            {"title": "ok2", "description": "fine", "due_date": "2026-02-30"},
        ]
        with mock.patch.object(ai, "break_down_task", return_value=items):
            r = self.post(f"{TASKS}{parent}/breakdown/")
        error = self.assertErrorShape(r, 502, "INVALID_BREAKDOWN_OUTPUT")
        self.assertEqual(
            {(d["field"], d["issue"]) for d in error["details"]},
            {
                ("items[1].title", "must not be empty"),
                ("items[2].due_date", "must be a valid date in YYYY-MM-DD format"),
            },
        )
        self.assertEqual(Task.objects.count(), 1)

    def test_breakdown_bad_shapes(self):
        parent = self.make_task()
        for bad in ([], "nope", [["x"]], [None], [{"title": 5, "description": "x"}]):
            with mock.patch.object(ai, "break_down_task", return_value=bad):
                r = self.post(f"{TASKS}{parent}/breakdown/")
            self.assertErrorShape(r, 502)
        with mock.patch.object(ai, "break_down_task", side_effect=RuntimeError("x")):
            self.assertErrorShape(self.post(f"{TASKS}{parent}/breakdown/"), 502, "BREAKDOWN_ERROR")
        self.assertEqual(Task.objects.count(), 1)


class IdempotencyTests(ApiTestCase):
    def test_create_same_key_same_body_creates_one_row(self):
        first = self.post(TASKS, valid_task(), key="k1")
        second = self.post(TASKS, valid_task(), key="k1")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(Task.objects.count(), 1)

    def test_body_key_order_does_not_matter(self):
        self.post(TASKS, {"title": "a", "description": "b"}, key="k")
        r = self.post(TASKS, {"description": "b", "title": "a"}, key="k")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Task.objects.count(), 1)

    def test_same_key_different_body_is_422(self):
        self.post(TASKS, valid_task(), key="k1")
        r = self.post(TASKS, valid_task(title="Other"), key="k1")
        self.assertErrorShape(r, 422, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(Task.objects.count(), 1)

    def test_same_key_on_another_endpoint_is_422(self):
        self.post(TASKS, valid_task(), key="k1")
        r = self.post(FROM_TEXT, {"text": "Buy milk"}, key="k1")
        self.assertErrorShape(r, 422, "IDEMPOTENCY_KEY_REUSED")

    def test_failed_request_is_not_stored_so_client_can_fix_and_retry(self):
        bad = self.post(TASKS, {"title": "", "description": ""}, key="k1")
        self.assertEqual(bad.status_code, 400)
        good = self.post(TASKS, valid_task(), key="k1")
        self.assertEqual(good.status_code, 201)
        self.assertEqual(Task.objects.count(), 1)

    def test_no_key_means_no_deduplication(self):
        self.post(TASKS, valid_task())
        self.post(TASKS, valid_task())
        self.assertEqual(Task.objects.count(), 2)

    def test_different_keys_create_different_rows(self):
        self.post(TASKS, valid_task(), key="a")
        self.post(TASKS, valid_task(), key="b")
        self.assertEqual(Task.objects.count(), 2)

    def test_from_text_replay_does_not_call_parser_again(self):
        body = {"text": "Buy milk tomorrow"}
        with mock.patch.object(ai, "parse_task_text", wraps=ai.parse_task_text) as parser:
            first = self.post(FROM_TEXT, body, key="t1")
            second = self.post(FROM_TEXT, body, key="t1")
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(Task.objects.count(), 1)

    def test_from_text_different_body_is_422(self):
        self.post(FROM_TEXT, {"text": "Buy milk"}, key="t1")
        r = self.post(FROM_TEXT, {"text": "Buy eggs"}, key="t1")
        self.assertErrorShape(r, 422, "IDEMPOTENCY_KEY_REUSED")

    def test_from_text_502_not_stored(self):
        bad = {"title": "", "description": "x"}
        with mock.patch.object(ai, "parse_task_text", return_value=bad):
            self.assertEqual(self.post(FROM_TEXT, {"text": "x"}, key="t1").status_code, 502)
        self.assertEqual(self.post(FROM_TEXT, {"text": "x"}, key="t1").status_code, 201)

    def test_breakdown_replay_does_not_duplicate_or_recall_breakdown(self):
        parent = self.post(TASKS, valid_task(title="A and B")).json()["id"]
        with mock.patch.object(ai, "break_down_task", wraps=ai.break_down_task) as fn:
            first = self.post(f"{TASKS}{parent}/breakdown/", key="b1")
            second = self.post(f"{TASKS}{parent}/breakdown/", key="b1")
        self.assertEqual(fn.call_count, 1)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(Task.objects.filter(parent=parent).count(), 2)

    def test_breakdown_same_key_other_task_is_422(self):
        a = self.post(TASKS, valid_task(title="A and B")).json()["id"]
        b = self.post(TASKS, valid_task(title="C and D")).json()["id"]
        self.post(f"{TASKS}{a}/breakdown/", key="b1")
        r = self.post(f"{TASKS}{b}/breakdown/", key="b1")
        self.assertErrorShape(r, 422, "IDEMPOTENCY_KEY_REUSED")


class ListQueryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        rows = [
            ("Pay rent", "monthly bill", "high", "2026-12-01", False),
            ("Buy milk", "dairy", "low", "2026-11-25", False),
            ("Pay phone bill", "monthly", "high", "2026-12-10", True),
            ("Write report", "quarterly", "high", "2026-11-30", False),
            ("Pay taxes", "annual", "medium", None, False),
        ]
        for title, desc, prio, due, done in rows:
            Task.objects.create(
                title=title, description=desc, priority=prio, due_date=due, completed=done
            )

    def titles(self, response):
        return [t["title"] for t in response.json()["results"]]

    def test_pagination_shape(self):
        r = self.client.get(TASKS, {"page_size": 2})
        body = r.json()
        self.assertEqual(body["count"], 5)
        self.assertEqual(len(body["results"]), 2)
        self.assertIsNotNone(body["next"])
        self.assertIsNone(body["previous"])

    def test_invalid_page_is_404_in_error_shape(self):
        self.assertErrorShape(self.client.get(TASKS, {"page": 99}), 404, "NOT_FOUND")

    def test_filter_search_order_paginate_together(self):
        params = {
            "priority": "high",
            "completed": "false",
            "search": "pay",
            "ordering": "-due_date",
            "page_size": 1,
        }
        page1 = self.client.get(TASKS, params).json()
        self.assertEqual(page1["count"], 1)  # only "Pay rent" is high + open + matches "pay"
        self.assertEqual(page1["results"][0]["title"], "Pay rent")

        # looser combination spanning 2 pages
        params = {"priority": "high", "ordering": "due_date", "page_size": 2}
        p1 = self.client.get(TASKS, params).json()
        p2 = self.client.get(TASKS, {**params, "page": 2}).json()
        self.assertEqual(p1["count"], 3)
        self.assertEqual(
            [t["title"] for t in p1["results"]] + [t["title"] for t in p2["results"]],
            ["Write report", "Pay rent", "Pay phone bill"],
        )

    def test_individual_filters(self):
        self.assertEqual(self.client.get(TASKS, {"completed": "true"}).json()["count"], 1)
        self.assertEqual(self.client.get(TASKS, {"due_before": "2026-11-30"}).json()["count"], 2)
        self.assertEqual(self.client.get(TASKS, {"due_after": "2026-12-01"}).json()["count"], 2)
        self.assertEqual(self.client.get(TASKS, {"search": "MILK"}).json()["count"], 1)
        self.assertEqual(self.client.get(TASKS, {"is_subtask": "false"}).json()["count"], 5)

    def test_invalid_filter_value_is_400_in_error_shape(self):
        self.assertErrorShape(self.client.get(TASKS, {"priority": "urgent"}), 400, "VALIDATION_ERROR")
        self.assertErrorShape(self.client.get(TASKS, {"due_before": "nope"}), 400, "VALIDATION_ERROR")

    def test_ordering(self):
        r = self.client.get(TASKS, {"ordering": "title"})
        self.assertEqual(self.titles(r)[0], "Buy milk")
        r = self.client.get(TASKS, {"ordering": "-title"})
        self.assertEqual(self.titles(r)[0], "Write report")
