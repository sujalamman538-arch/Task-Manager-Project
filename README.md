# Task Manager API

A Django REST Framework API that turns plain English like
*"Remind me to pay rent on the 1st of every month"* into a structured task.
It also supports subtasks, retry-safe creation with `Idempotency-Key`, one consistent
error format, and filtering, search, ordering and pagination.

The text-to-task step is a plain function (`parse_task_text` in `tasks/ai.py`), so the
current rule-based version can be swapped for an LLM later.

## Setup

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your Postgres credentials
```

Create the database. With a local Postgres:

```bash
createdb task_manager
```

Or with Docker:

```bash
docker run --name taskdb -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=task_manager \
  -p 5432:5432 -d postgres:16
```

Then:

```bash
python manage.py migrate
python manage.py runserver     # http://127.0.0.1:8000/api/tasks/
python manage.py test
```

`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` and `DB_PORT` are read from `.env`.

**LLM key (optional).** The default parser is rule-based and needs no key. To use the
LLM parser slot, add `TASK_PARSER=llm` and `LLM_API_KEY=...` to `.env`. If the key is
missing, `POST /api/tasks/from-text/` returns 503 in the standard error format.
`.env` is git-ignored, so never commit a key.

## Task fields

| Field | Rules |
| --- | --- |
| `title` | required, string, not empty, max 255 |
| `description` | required, string, not empty |
| `completed` | boolean, default `false` |
| `due_date` | `YYYY-MM-DD` or `null`, must be a real date (`2026-02-30` is rejected) |
| `priority` | `low`, `medium` (default) or `high` |
| `recurrence` | `daily`, `weekly`, `monthly`, `yearly` or empty |
| `parent` | read-only id of the parent task, `null` for top-level tasks |

Deleting a task deletes its subtasks.

## Error format

Every error (400, 404, 405, 422, 502, 503) has this shape. `code` and `message` are
strings, `details` is a list.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [{ "field": "title", "issue": "must not be empty" }]
  }
}
```

Codes: `VALIDATION_ERROR`, `INVALID_JSON`, `NOT_FOUND`, `SUBTASK_NOT_ALLOWED`,
`IDEMPOTENCY_KEY_REUSED` (422), `PARSER_ERROR` and `INVALID_PARSER_OUTPUT` (502),
`BREAKDOWN_ERROR` and `INVALID_BREAKDOWN_OUTPUT` (502), `LLM_UNAVAILABLE` (503).

## Endpoints

| Method | URL | What it does |
| --- | --- | --- |
| POST | `/api/tasks/` | create a task |
| GET | `/api/tasks/` | list tasks |
| GET, PUT, PATCH, DELETE | `/api/tasks/{id}/` | retrieve, replace, update, delete |
| POST | `/api/tasks/from-text/` | create a task from plain English |
| GET, POST | `/api/tasks/{id}/subtasks/` | list or create subtasks |
| POST | `/api/tasks/{id}/breakdown/` | split a task into subtasks |

### Create a task

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/ -H 'Content-Type: application/json' \
  -d '{"title": "Buy milk", "description": "2 litres", "priority": "low", "due_date": "2026-12-01"}'
```

`201 Created`

```json
{
  "id": 1,
  "title": "Buy milk",
  "description": "2 litres",
  "completed": false,
  "due_date": "2026-12-01",
  "priority": "low",
  "recurrence": "",
  "parent": null,
  "created_at": "2026-11-21T08:42:59.286487Z",
  "updated_at": "2026-11-21T08:42:59.286503Z"
}
```

### Validation error

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/ -H 'Content-Type: application/json' \
  -d '{"title": "", "description": "x", "due_date": "2026-02-30", "completed": "yes", "priority": "urgent"}'
```

`400 Bad Request`

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [
      { "field": "title", "issue": "must not be empty" },
      { "field": "completed", "issue": "must be a boolean (true or false)" },
      { "field": "due_date", "issue": "must be a valid date in YYYY-MM-DD format" },
      { "field": "priority", "issue": "must be one of: low, medium, high" }
    ]
  }
}
```

### List, filter, search, order, paginate

Query parameters: `completed`, `priority`, `recurrence`, `due_date`, `due_before`,
`due_after`, `parent`, `is_subtask`, `search` (title or description),
`ordering` (`id`, `title`, `due_date`, `created_at`, `updated_at`, prefix `-` for
descending), `page` and `page_size` (default 10, max 100). They can be combined:

```bash
curl 'http://127.0.0.1:8000/api/tasks/?priority=high&completed=false&search=pay&ordering=-due_date&page=1&page_size=5'
```

`200 OK`

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 4,
      "title": "Pay rent",
      "description": "monthly bill",
      "completed": false,
      "due_date": "2026-12-01",
      "priority": "high",
      "recurrence": "",
      "parent": null,
      "created_at": "2026-11-21T08:50:00Z",
      "updated_at": "2026-11-21T08:50:00Z"
    }
  ]
}
```

### Retrieve, update, delete

`GET /api/tasks/{id}/` returns the task. `PUT` needs `title` and `description`, `PATCH`
accepts any fields, `DELETE` returns `204`. An unknown id returns:

```json
{ "error": { "code": "NOT_FOUND", "message": "Task with id '999' not found", "details": [] } }
```

### Create a task from text

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/from-text/ -H 'Content-Type: application/json' \
  -d '{"text": "Remind me to pay rent on the 1st of every month"}'
```

`201 Created` (sent on 2026-11-21)

```json
{
  "id": 1,
  "title": "Pay rent",
  "description": "Remind me to pay rent on the 1st of every month",
  "completed": false,
  "due_date": "2026-12-01",
  "priority": "medium",
  "recurrence": "monthly",
  "parent": null,
  "created_at": "2026-11-21T08:42:59.286487Z",
  "updated_at": "2026-11-21T08:42:59.286503Z"
}
```

The parser output goes through `TaskSerializer` exactly like user input. If the parser
raises, the API returns 502 `PARSER_ERROR`. If its output is invalid (empty title, wrong
type, impossible date), the API returns 502 `INVALID_PARSER_OUTPUT` and saves nothing:

```json
{
  "error": {
    "code": "INVALID_PARSER_OUTPUT",
    "message": "Parser returned invalid task data",
    "details": [{ "field": "title", "issue": "must not be empty" }]
  }
}
```

The rule-based parser understands `today`, `tomorrow`, `day after tomorrow`,
`in 3 days/weeks/months`, `next week`, `next month`, weekdays (`on friday`,
`every monday`), `the 1st` / `the 15th`, `March 5th`, `5 December`, ISO dates, and
`daily`, `weekly`, `monthly`, `yearly`, `every day/week/month/year`.

**Swapping in an LLM.** Keep this signature and return shape in `tasks/ai.py`:

```python
def parse_task_text(text: str, today: datetime.date) -> dict:
    """
    Returns {
        "title": str,                  # required, not empty
        "description": str,            # required, not empty
        "due_date": "YYYY-MM-DD" | None,
        "recurrence": "daily" | "weekly" | "monthly" | "yearly",   # optional key
    }
    Any exception becomes a 502.
    """
```

### Subtasks

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/1/subtasks/ -H 'Content-Type: application/json' \
  -d '{"title": "Check bank balance", "description": "Before the 1st"}'
```

`201 Created` returns the subtask with `"parent": 1`. `GET /api/tasks/1/subtasks/`
returns a plain array of that task's subtasks. A subtask cannot have subtasks and cannot
be broken down; both return `400 SUBTASK_NOT_ALLOWED`.

### Breakdown

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/1/breakdown/
```

`break_down_task(task)` splits the title on commas and "and"
(`"Plan trip, book flights and pack bags"` becomes three subtasks). A title with nothing
to split uses a template: *Plan: X*, *Do: X*, *Review: X*. Subtasks inherit the parent's
`due_date` and `priority`. The response is `201` with a list of the created subtasks.

Every item is validated first. If any item is invalid, none are saved and the API returns
`502 INVALID_BREAKDOWN_OUTPUT` with details like
`{"field": "items[1].title", "issue": "must not be empty"}`.

## Idempotent creation

Send an optional `Idempotency-Key` header on `POST /api/tasks/`,
`POST /api/tasks/from-text/` and `POST /api/tasks/{id}/breakdown/`.

- Same key and same request: the stored response is returned with an
  `Idempotent-Replayed: true` header. Nothing is created and the parser is not called again.
- Same key with a different body (or a different endpoint or task): `422 IDEMPOTENCY_KEY_REUSED`.
- Only successful responses are stored, so after a 400 or 502 the client can fix the
  input and retry with the same key.

The store is a dict in `tasks/idempotency.py`. It lives in memory, so it is cleared when
the server restarts.

## Tests

`python manage.py test` runs 59 tests covering CRUD, every validation rule, the error
format on every error path, the parser, bad parser output (nothing saved), the 503 case,
subtasks and cascade delete, breakdown, idempotency, and filter + search + order +
paginate together.

`postman/Task_Manager.postman_collection.json` has 53 requests grouped by feature. Import it
into Postman or Echo API and set the `parentId` and `subtaskId` variables from the ids
returned by "Create parent" and "Create subtask".

`openapi.yaml` is the hand-written OpenAPI description of the endpoints and the error format.

### What the AI-generated checklist missed

The first checklist was mostly happy paths: one create, list, get, update, delete, one
`from-text` call, one subtask, one breakdown, one idempotent retry. I added:

- Validation cases: blank strings, missing fields, `title: 123`, `completed: "yes"` or `1`
  (DRF accepts these, so I made the boolean field strict), `2026-02-30`, `2026-2-3`,
  an invalid priority.
- Unknown ids on every endpoint, a non-numeric id, a page past the end, malformed JSON,
  and a wrong HTTP method, all with the same error format.
- Idempotency: same key with a different body, same key on another endpoint, a key
  reused after a failed request, and the parser not being called again on a replay.
- Bad parser output: empty title, wrong types, impossible date, invalid priority or
  recurrence, a non-dict result, and the parser raising. Each gives 502 and saves no row.
- A subtask of a subtask, a breakdown of a subtask, a client sending `parent` in the body
  (now read-only), cascade delete, and one invalid breakdown item saving nothing.
- Ordering by `due_date` could repeat or skip rows between pages, so `id` is added as a
  tie-breaker.

I also checked three things against the real Postgres database: the same `from-text`
request sent twice with one key leaves 1 row, a parser returning an empty title gives 502
and adds 0 rows, and deleting a parent with 3 subtasks leaves 0 subtasks.
