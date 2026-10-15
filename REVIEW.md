# REVIEW.md

Defects found in the starter `app/main.py` (line numbers refer to the original starter file).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Module, `MongoClient(...)` (l.22) | Client is not `tz_aware`, so PyMongo returns naive datetimes; later maths mixes naive local and UTC times. | Read a seeded record and compute its IST date: it is off by 5:30 on a non-UTC server. | `MongoClient(..., tz_aware=True)`; every instant is an aware UTC datetime. |
| 2 | Module | No indexes are created, so `emp_code` uniqueness and one-record-per-day are not guaranteed and every query is a COLLSCAN. | `explain` on `GET /attendance` shows COLLSCAN; parallel duplicate creates both succeed. | Indexes created idempotently at startup (lifespan): unique `emp_code`, unique `(emp_code, date)`, `(date desc, emp_code)`, `(department, joined_on)`, `joined_on`. |
| 3 | `compute_late_minutes` (l.33) | `punch_in.replace(hour=..)` sets the shift start in whatever zone `punch_in` is in (UTC/naive), not IST. | Punch at 09:45 IST (04:15 UTC) gives a negative difference, so late = 0. | Build the shift start as `datetime.combine(attendance_date, shift_start, IST)`. |
| 4 | `compute_late_minutes` (l.37) | Grace check uses whole minutes `> 10`, so 09:40:30 (strictly more than 10:00) gives 0 instead of 10. | Shift 09:30, punch 09:40:01 -> expected 10, got 0. | Compare seconds: late if `diff > 600`, then `diff // 60`. |
| 5 | `compute_late_minutes` | Overnight shifts not handled: a 00:30 punch for a 22:00 shift is compared to 22:00 of the same day. | Shift 22:00-06:00, punch 00:30 -> negative, 0 instead of 150. | Shift start is taken on the attendance date (R1 = previous day for that punch). |
| 6 | `compute_work_hours` (l.40) | `round()` is float / banker's rounding, not half-up. | 4h29m42s = 4.495 h -> `round` gives 4.49, expected 4.50 (also flips `half_day`). | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 7 | `compute_overtime` (l.44) | No 30-minute minimum; overnight shift end is put on the start date instead of the next day. | Punch-out 18:35 -> 5 min overtime instead of 0; overnight 06:40 out -> huge overtime. | `minutes >= 30` else 0; shift end +1 day when `shift_end <= shift_start`. |
| 8 | `EmployeeIn` (l.53) | No validation: any `emp_code`, email, shift time format, invalid `joined_on`, `shift_start == shift_end` accepted. | `POST /employees` with `emp_code: "x"` or `joined_on: "2026-02-30"` -> 201 instead of 422. | Pydantic constraints from the contract + validator for date and equal shift times. |
| 9 | `PunchInIn` (l.63) | `punched_at: Optional[int]` accepts seconds, and lax mode coerces strings/floats/bools; `status` accepts any string (e.g. `ABSENT`). | `punched_at: 1783312500` (seconds) or `"1783312500000"` -> 201; `status: "FOO"` stored. | Strict int with range 1e11..4102444800000; status limited to `PRESENT/WFH/ON_DUTY`. |
| 10 | `health` (l.72) | Always returns 200, never checks MongoDB. | Stop MongoDB: `/health` still 200. | `ping` the server; 503 on failure. |
| 11 | `create_employee` (l.79-82) | Check-then-insert race: two parallel creates both pass `find_one` and both insert. | 10 parallel POSTs with the same `emp_code` -> several 201s. | Rely on the unique index; catch `DuplicateKeyError` -> 409. |
| 12 | `create_employee` (l.82) | `created_at = datetime.now()` is naive local time and is returned as a datetime, not epoch ms. | Response has `"created_at": "2026-..T.."` string. | Store aware UTC (truncated to seconds) and return epoch ms. |
| 13 | `list_employees` (l.93) | `skip = page * page_size`: page 1 skips the first page. | 3 employees, `page=1` -> empty list. | `skip = (page - 1) * page_size`. |
| 14 | `list_employees` (l.94) | `total` counts the whole collection, ignoring the department filter. | `?department=Sales` -> total = all employees. | `count_documents(q)` with the same filter. |
| 15 | `list_employees` | No sort by `emp_code`, no bounds on `page` / `page_size` (page 0, page_size 10000). | Order differs between calls; `page_size=1000` accepted. | `.sort("emp_code")`, `page >= 1`, `1 <= page_size <= 100`. |
| 16 | `punch_in` (l.101) | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises -> 500. | Punch in `EMP9999` -> 500 instead of 404. | Look up employee first, 404 if missing. |
| 17 | `punch_in` (l.102-103) | `fromtimestamp` uses the server's local zone; date is not the IST date; no overnight rule (R1); milliseconds not truncated. | Punch 2026-07-06 01:00 IST on a UTC server -> date 2026-07-05. | Convert ms -> UTC (truncated to whole seconds) -> IST date, with overnight rule. |
| 18 | `punch_in` (l.104) | Check-then-insert race for the same employee/date. | 10 parallel punch-ins -> more than one 201. | Unique `(emp_code, date)` index + `DuplicateKeyError` -> 409. |
| 19 | `punch_in` (l.119) | Response leaks `_id` as `id` and returns datetimes, not epoch ms. | Response contains `"id"` and ISO strings. | Common `serialize_record()`; no ids, instants as ms. |
| 20 | `list_attendance` (l.144) | Loads the whole collection into Python and sorts/paginates in memory. | Slow / memory heavy at 100k records; explain shows COLLSCAN. | `find(q).sort(...).skip().limit()` backed by `(date desc, emp_code)` and `(emp_code, date)` indexes. |
| 21 | `list_attendance` (l.145) | Sort is only by date; ties are not ordered by `emp_code`. | Same-date rows come back in random order. | Sort `date desc, emp_code asc`. |
| 22 | `list_attendance` | No validation: `date_from > date_to`, bad dates, unknown `status`, `page=0` all accepted. | `?status=FOO` -> 200, `?page=0` -> wrong slice. | 422 for each; enum for status. |
| 23 | `list_attendance` (l.149) | Exposes `_id` as `id`; datetimes not ms; legacy records without `history` / `half_day` returned without them. | Legacy sample record 9 lacks `half_day` in the response. | `serialize_record()` defaults `history=[]`, `half_day=false`. |

**Looked at and decided are not defects**

- `load_dotenv()` does not override variables already in the environment (default `override=False`), so real env vars win, as required.
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct (India has no DST).
- Building the date range filter on string `date` fields is fine: `YYYY-MM-DD` strings sort the same way as dates.
- `insert_one(doc)` adding `_id` to the dict and then popping it is fine, just fragile, so I replaced it with explicit serialisation.
