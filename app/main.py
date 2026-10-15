"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Config & Database
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False by default, so real environment variables take priority over .env

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENCE = ["ABSENT", "LEAVE"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_TREND_DAYS = 92

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT = Literal["PRESENT", "WFH", "ON_DUTY"]
# Epoch milliseconds: strict int (no floats / strings / bools), and anything below 1e11 looks like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
ShiftTime = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def unprocessable(msg: str, loc: list) -> HTTPException:
    """422 with the same body shape FastAPI uses for validation errors."""
    return HTTPException(422, [{"type": "value_error", "loc": loc, "msg": msg}])


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to whole seconds (R1). Integer maths, no float error."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """Current time truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def half_up(value: Optional[float], places: int = 2) -> Optional[float]:
    """Round half-up (not banker's). round(x, 10) first removes binary float noise like 8.12499999999."""
    if value is None:
        return None
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(value, 10))).quantize(quant, rounding=ROUND_HALF_UP))


def parse_date(value: str, loc: list) -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise unprocessable("must be a date in YYYY-MM-DD format", loc)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable("must be a valid calendar date", loc)


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31'). Dates are strings, so string range queries work."""
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_weekdays(start: date, end: date) -> int:
    """Number of Mon-Fri days in [start, end]."""
    if end < start:
        return 0
    total = (end - start).days + 1
    full_weeks, rest = divmod(total, 7)
    days = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            days += 1
    return days


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings compare correctly


# --------------------------------------------------------------------------- #
# Business rules (R1 - R5)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts a punch before shift_end
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(day: str, emp: dict) -> datetime:
    return datetime.combine(date.fromisoformat(day), parse_hhmm(emp["shift_start"]), IST)


def shift_end_dt(day: str, emp: dict) -> datetime:
    end = datetime.combine(date.fromisoformat(day), parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)  # overnight shift ends on the next calendar day
    return end


def compute_late_minutes(punch_in: datetime, day: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    diff = int((punch_in - shift_start_dt(day, emp)).total_seconds())
    return diff // 60 if diff > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: str, emp: dict) -> int:
    """R3: floor minutes after shift end, only if >= 30."""
    minutes = int((punch_out - shift_end_dt(day, emp)).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up, using Decimal (exact)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(status: str, punch_in, punch_out, day: str, emp: dict) -> dict:
    """All four derived values for a record, following the table in DATA_MODEL.md."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp)
        out["half_day"] = Decimal(str(wh)) < HALF_DAY_HOURS  # R5 uses the rounded value
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @model_validator(mode="after")
    def check(self):
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceT = "PRESENT"


class PunchOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMs] = None


class Regularize(BaseModel):
    # extra="ignore": derived fields sent by a client are silently ignored (contract says so)
    model_config = ConfigDict(extra="ignore")

    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]

    @model_validator(mode="before")
    @classmethod
    def no_explicit_nulls(cls, data):
        # These fields are optional but not nullable: {"punch_in": null} is a 422, not "unchanged".
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} may be omitted but not null")
        return data


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _ms_if_dt(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_record(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        changes = {
            field: {"from": _ms_if_dt(c.get("from")), "to": _ms_if_dt(c.get("to"))}
            for field, c in (h.get("changes") or {}).items()
        }
        history.append({"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
INDEXES = [
    # employees
    (employees_col, [("emp_code", ASCENDING)], {"unique": True, "name": "uniq_emp_code"}),
    (employees_col, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "department_joined_on"}),
    (employees_col, [("joined_on", ASCENDING)], {"name": "joined_on"}),
    # attendance_logs
    (logs_col, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uniq_emp_code_date"}),
    (logs_col, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp_code"}),
]


def ensure_indexes() -> None:
    for col, keys, opts in INDEXES:
        try:
            col.create_index(keys, **opts)
        except PyMongoError as exc:  # never crash startup; /health will report a dead DB
            print(f"WARNING: could not create index {opts['name']}: {exc}")
            if not isinstance(exc, DuplicateKeyError):
                return  # database unreachable: don't wait for every index to time out


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees_col.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = employees_col.count_documents(q)
    cursor = employees_col.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Unique index on (emp_code, date): of N concurrent inserts exactly one succeeds.
        logs_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    rec = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        raise unprocessable("punched_at must be after punch_in and within 24 hours", ["body", "punched_at"])

    derived = derived_fields(rec["status"], rec["punch_in"], ts, rec["date"], emp)
    # Atomic: the filter only matches while punch_out is still null, so only one of two
    # simultaneous punch-outs can win; the other gets None -> 409.
    updated = logs_col.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    df = parse_date(date_from, ["query", "date_from"]) if date_from is not None else None
    dt = parse_date(date_to, ["query", "date_to"]) if date_to is not None else None
    if df and dt and df > dt:
        raise unprocessable("date_from must be on or before date_to", ["query", "date_from"])
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = logs_col.count_documents(q)
    # Sorting, skipping and limiting happen in MongoDB (backed by indexes), not in Python.
    cursor = logs_col.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: Regularize):  # noqa: A002 (path name fixed by contract)
    parse_date(date, ["path", "date"])
    emp = get_employee_or_404(emp_code)
    rec = logs_col.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    new_status = body.status or old["status"]
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if new_status in NON_PRESENCE:
        if new_in is not None or new_out is not None:
            raise unprocessable(f"{new_status} cannot have punch times", ["body"])
        final_in = final_out = None
    else:
        final_in = new_in if new_in is not None else old["punch_in"]
        final_out = new_out if new_out is not None else old["punch_out"]
        if final_in is None:
            raise unprocessable("a presence status requires punch_in", ["body", "punch_in"])
        if new_in is not None and attendance_date(new_in, emp) != date:
            raise unprocessable("punch_in must stay on the record's attendance date", ["body", "punch_in"])
        if final_out is not None and (final_out <= final_in or final_out - final_in > timedelta(hours=24)):
            raise unprocessable("punch_out must be after punch_in and within 24 hours", ["body", "punch_out"])

    new = {"status": new_status, "punch_in": final_in, "punch_out": final_out}
    new.update(derived_fields(new_status, final_in, final_out, date, emp))

    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        raise unprocessable("the request changes nothing", ["body"])

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only update if the record is still exactly what we read
    # (same punch times/status and same number of history entries). A concurrent editor
    # that got there first makes this match nothing -> 409, so no history entry is lost.
    n = len(rec.get("history") or [])
    history_guard = (
        {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]} if n == 0 else {"history": {"$size": n}}
    )
    guard = {
        "_id": rec["_id"],
        "status": old["status"],
        "punch_in": old["punch_in"],
        "punch_out": old["punch_out"],
        **history_guard,
    }
    updated = logs_col.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Aggregation pipeline builders
# --------------------------------------------------------------------------- #
def _f(prefix: str, field: str) -> str:
    """Field reference: _f('$', 'status') -> '$status'; _f('$$l.', 'status') -> '$$l.status'."""
    return f"{prefix}{field}"


def expr_present_value(p: str = "$") -> dict:
    """R5/R6: 1 for a presence-status record, 0.5 if it is a half day, else 0."""
    return {
        "$cond": [
            {"$in": [_f(p, "status"), PRESENCE]},
            {"$cond": [{"$eq": [{"$ifNull": [_f(p, "half_day"), False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def expr_is_weekday(p: str = "$") -> dict:
    """R7: Mon-Fri, from the 'YYYY-MM-DD' string."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": _f(p, "date"), "format": "%Y-%m-%d"}}}, 5]}


def expr_is_late(p: str = "$") -> dict:
    return {"$gt": [{"$ifNull": [_f(p, "late_minutes"), 0]}, 0]}


def expr_count_if(cond: dict) -> dict:
    return {"$cond": [cond, 1, 0]}


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # index uniq_emp_code_date
        {
            "$group": {
                "_id": None,
                # present_days counts Mon-Fri only; weekend records still count for late/overtime
                "present_days": {"$sum": {"$cond": [expr_is_weekday(), expr_present_value(), 0]}},
                "leave_days": {"$sum": expr_count_if({"$eq": ["$status", "LEAVE"]})},
                "late_count": {"$sum": expr_count_if(expr_is_late())},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before the end of the month
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$isNumber": "$$l.work_hours"}]}
    return [
        # Start from employees so people with zero logs are still counted in headcount.
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],  # uses uniq_emp_code_date
                "as": "logs",
            }
        },
        # Per-employee totals (an employee with no logs gets zeros, but still exists as a row).
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [expr_is_weekday("$$l."), expr_present_value("$$l."), 0]},
                        }
                    }
                },
                "hours_sum": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [has_hours, "$$l.work_hours", 0]}}}
                },
                "hours_n": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr_count_if(has_hours)}}},
                "late_count": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": expr_count_if(expr_is_late("$$l."))}}
                },
                "total_late_minutes": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}
                },
                "leave_count": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": expr_count_if({"$eq": ["$$l.status", "LEAVE"]})}}
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {"input": "$logs", "as": "l", "in": expr_count_if({"$eq": ["$$l.status", "ON_DUTY"]})}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                # average over RECORDS: total hours / number of records (not an average of averages)
                "hours_sum": {"$sum": "$hours_sum"},
                "hours_n": {"$sum": "$hours_n"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},  # index date_desc_emp_code
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # Inner join: logs whose employee does not exist are dropped by $unwind.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # Standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking, so ties at the cutoff are kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def round_expr(expr, places: int) -> dict:
    """Half-up rounding inside MongoDB ($round is banker's rounding). Tiny epsilon absorbs float noise."""
    factor = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5 + 1e-9]}}, factor]}


def build_department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    s_from, s_to = d_from.isoformat(), d_to.isoformat()
    lower = datetime.combine(d_from, time(0), timezone.utc)
    upper = datetime.combine(d_to + timedelta(days=1), time(0), timezone.utc)  # $densify upper bound is exclusive
    return [
        {"$match": {"department": department, "joined_on": {"$lte": s_to}}},  # index department_joined_on
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": s_from, "$lte": s_to}}},
                    {"$project": {"_id": 0, "date": 1, "p": expr_present_value(), "l": expr_count_if(expr_is_late())}},
                ],
                "as": "logs",
            }
        },
        # Turn every employee into "events": one +1 headcount event on the day they count from
        # (max(joined_on, from)), plus one event per log with its present value and late flag.
        {
            "$project": {
                "_id": 0,
                "events": {
                    "$concatArrays": [
                        [{"day": {"$max": ["$joined_on", s_from]}, "hc": 1, "p": 0, "l": 0}],
                        {"$map": {"input": "$logs", "as": "x", "in": {"day": "$$x.date", "hc": 0, "p": "$$x.p", "l": "$$x.l"}}},
                    ]
                },
            }
        },
        {"$unwind": "$events"},
        {"$replaceRoot": {"newRoot": "$events"}},
        {"$group": {"_id": "$day", "hc": {"$sum": "$hc"}, "p": {"$sum": "$p"}, "l": {"$sum": "$l"}}},
        {
            "$project": {
                "_id": 0,
                "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "hc": 1,
                "p": 1,
                "l": 1,
            }
        },
        # Gap filling: exactly one document per calendar day in [from, to].
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        # Headcount on a day = running total of join events up to that day (R9).
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$p", 0]},
                "late_count": {"$ifNull": ["$l", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_expr({"$divide": ["$present_count", {"$max": ["$headcount", 1]}]}, 4),
                        None,
                    ]
                }
            }
        },
        # 7-day moving average over rows inside the range only; $avg ignores nulls, returns null if all null.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def validate_trend_range(d_from: str, d_to: str) -> tuple[date, date]:
    f = parse_date(d_from, ["query", "from"])
    t = parse_date(d_to, ["query", "to"])
    if t < f:
        raise unprocessable("'to' must be on or after 'from'", ["query", "to"])
    if (t - f).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", ["query", "to"])
    return f, t


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    rows = list(logs_col.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}

    start, end = month_bounds(month)
    first = max(date.fromisoformat(start), date.fromisoformat(emp["joined_on"]))  # R7 mid-month joiners
    working_days = count_weekdays(first, date.fromisoformat(end))
    present = r.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(present / working_days * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in employees_col.aggregate(build_department_summary_pipeline(month, department)):
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": half_up(r["present_days"], 2),
                "avg_work_hours": half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = []
    for r in logs_col.aggregate(build_late_leaderboard_pipeline(month, limit, department)):
        r["rank"] = int(r["rank"])
        r["total_late_minutes"] = int(r["total_late_minutes"])
        r["late_count"] = int(r["late_count"])
        items.append(r)
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d_from, d_to = validate_trend_range(from_, to)
    if employees_col.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in employees_col.aggregate(build_department_trend_pipeline(department, d_from, d_to)):
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": int(r["headcount"]),
                "present_count": r["present_count"],
                "late_count": int(r["late_count"]),
                "attendance_rate": r["attendance_rate"],
                "moving_avg_7d": half_up(r["moving_avg_7d"], 4),
            }
        )
    if not items:
        # Only when nobody in the department has joined by `to`: the pipeline then has no input
        # documents for $densify to fill, and every day is simply headcount 0 with null rates.
        days = (d_to - d_from).days + 1
        items = [
            {
                "date": (d_from + timedelta(days=i)).isoformat(),
                "is_working_day": (d_from + timedelta(days=i)).weekday() < 5,
                "headcount": 0,
                "present_count": 0,
                "late_count": 0,
                "attendance_rate": None,
                "moving_avg_7d": None,
            }
            for i in range(days)
        ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #
def _require(value, name: str):
    if value is None:
        raise unprocessable(f"query parameter '{name}' is required for this endpoint", ["query", name])
    return value


def _explain_aggregate(collection: str, pipeline: list) -> dict:
    return db.command(
        "explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats"
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = build_attendance_query(emp_code, date_from, date_to, status)
        cmd = {
            "find": collection,
            "filter": q,
            "sort": {k: v for k, v in ATTENDANCE_SORT},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        result = db.command("explain", cmd, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        collection = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(_require(emp_code, "emp_code"), _require(month, "month"))
        result = _explain_aggregate(collection, pipeline)
    elif endpoint == "department_summary":
        collection = "employees"
        result = _explain_aggregate(collection, build_department_summary_pipeline(_require(month, "month"), department))
    elif endpoint == "late_leaderboard":
        collection = "attendance_logs"
        result = _explain_aggregate(
            collection, build_late_leaderboard_pipeline(_require(month, "month"), limit, department)
        )
    else:  # department_trend
        collection = "employees"
        d_from, d_to = validate_trend_range(_require(from_, "from"), _require(to, "to"))
        result = _explain_aggregate(
            collection, build_department_trend_pipeline(_require(department, "department"), d_from, d_to)
        )
    # json_util turns BSON-only types (Timestamp, ObjectId, Int64...) into plain JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
