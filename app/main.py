"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read, but real environment variables win)

How this file is laid out:
  1. config and database handle
  2. time helpers and the business rules R1 - R5 (pure functions, no database)
  3. request models and query validation
  4. serializers (Mongo document -> API shape)
  5. index creation at startup
  6. employee and attendance endpoints
  7. analytics pipelines + endpoints
  8. admin explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Config
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes instead of naive ones,
# so we never mix naive and aware values when we subtract them.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=4000)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_PUNCH_GAP = timedelta(hours=24)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# [0-9] instead of \d on purpose: \d also matches non-ASCII digits.
DAY_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def as_utc(value: datetime) -> datetime:
    """Make any datetime from the database timezone-aware (UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def whole_seconds(value: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return as_utc(value).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(value: datetime) -> int:
    return (as_utc(value) - EPOCH) // timedelta(milliseconds=1)


def ms_or_none(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


def now_whole_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_day(value: str) -> date:
    """Strict YYYY-MM-DD. Raises ValueError for things like 2026-02-30 or 20260706."""
    if not DAY_RE.fullmatch(value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)


def hhmm(value: str) -> day_time:
    hour, minute = value.split(":")
    return day_time(int(hour), int(minute))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def ist_at(day: str, clock: str) -> datetime:
    """The instant when `clock` (HH:MM, IST) happens on the IST calendar date `day`."""
    d = date.fromisoformat(day)
    t = hhmm(clock)
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in, except for overnight shifts, where a
    punch-in earlier than shift_end belongs to the shift that started the day before."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def late_minutes_for(punch_in: datetime, day: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes
    (floor) counted from shift_start, not from the end of the grace period."""
    seconds = int((punch_in - ist_at(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes_for(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, counted only when that is at least 30."""
    end = ist_at(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)  # an overnight shift ends on the next calendar day
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def half_up(value: Decimal, places: int) -> Decimal:
    """Round half up. Python's round() is banker's rounding and works on binary floats."""
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def work_hours_for(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(half_up(Decimal(seconds) / Decimal(3600), 2))


def punch_gap_problem(punch_in: datetime, punch_out: datetime) -> Optional[str]:
    if punch_out <= punch_in:
        return "punch_out must be after punch_in"
    if punch_out - punch_in > MAX_PUNCH_GAP:
        return "punch_out must be within 24 hours of punch_in"
    return None


def derive_fields(emp: dict, day: str, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """All the stored, computed fields of a presence record (R2 - R5)."""
    fields = {
        "late_minutes": late_minutes_for(punch_in, day, emp["shift_start"]),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = work_hours_for(punch_in, punch_out)
        fields["work_hours"] = hours
        fields["overtime_minutes"] = overtime_minutes_for(
            punch_out, day, emp["shift_start"], emp["shift_end"]
        )
        fields["half_day"] = Decimal(str(hours)) < HALF_DAY_LIMIT
    return fields


def count_weekdays(start: date, end: date) -> int:
    """R7: Monday - Friday days in [start, end]."""
    days = max((end - start).days + 1, 0)
    return sum(1 for i in range(days) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Request models and validation helpers
# --------------------------------------------------------------------------- #
# strict=True rejects floats ("1.7e12"), strings and booleans; the bounds reject
# values that look like seconds (below 1e11) or are absurdly far away.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


def bad_input(where: str, name: str, message: str):
    """Raise a 422 that has the same shape as FastAPI's own validation errors."""
    raise HTTPException(
        status_code=422,
        detail=[{"loc": [where, name], "msg": message, "type": "value_error"}],
    )


def check_day(value: str, where: str, name: str) -> date:
    try:
        return parse_day(value)
    except ValueError:
        bad_input(where, name, "must be a valid date in YYYY-MM-DD format")


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        bad_input("query", "month", "year out of range")
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_real_date(cls, value: str) -> str:
        parse_day(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # "omitted" means unchanged; an explicit null is not allowed by the contract.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN)]
DayParam = Annotated[Optional[str], Query(pattern=DAY_PATTERN)]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# 4. Serializers
# --------------------------------------------------------------------------- #
def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


def history_to_api(entry: dict) -> dict:
    changes = {}
    for field, pair in (entry.get("changes") or {}).items():
        before, after = pair.get("from"), pair.get("to")
        if field in ("punch_in", "punch_out"):  # dates inside nested objects too
            before = ms_or_none(before)
            after = ms_or_none(after)
        changes[field] = {"from": before, "to": after}
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def record_to_api(doc: dict) -> dict:
    """Seeded documents may miss late/overtime/half_day/history: missing means 0/false/[]."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup, safe to run repeatedly)
# --------------------------------------------------------------------------- #
def index_specs():
    return [
        # employees
        ("employees", [("emp_code", 1)], {"unique": True, "name": "emp_code_unique"}),
        ("employees", [("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
        ("employees", [("department", 1), ("joined_on", 1)], {"name": "dept_joined_on"}),
        ("employees", [("joined_on", 1)], {"name": "joined_on"}),
        # attendance_logs
        ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True, "name": "emp_date_unique"}),
        ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": "date_desc_emp"}),
        ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
        ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {"name": "emp_punch_in"}),
    ]


indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists is a no-op."""
    global indexes_ready
    failed = False
    for collection, keys, options in index_specs():
        try:
            db[collection].create_index(keys, **options)
        except PyMongoError:
            # Do not stop the whole app because one index failed (for example a unique
            # index over dirty data). Log it loudly instead.
            failed = True
            log.exception("could not create index %s on %s", options.get("name"), collection)
    indexes_ready = not failed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Try briefly at startup. If MongoDB is not up yet, /health finishes the job later.
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        log.warning("MongoDB not reachable at startup; indexes will be created once it answers")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def root():
    """Provide a useful response when the server URL is opened directly."""
    return {
        "name": app.title,
        "version": app.version,
        "status": "ok",
        "health": "/health",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.exception_handler(PyMongoError)
def database_error_handler(_request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def find_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Employees and attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    if not indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created = datetime.now(UTC)
    created = created.replace(microsecond=created.microsecond // 1000 * 1000)  # Mongo keeps ms
    doc = body.model_dump()
    doc["created_at"] = created
    try:
        # The unique index on emp_code decides who wins when two requests race.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = find_employee(body.emp_code)
    punched = ms_to_dt(body.punched_at) if body.punched_at is not None else now_whole_seconds()
    day = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes_for(punched, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # No "check then insert": the unique (emp_code, date) index lets exactly one
        # of several simultaneous requests succeed.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = find_employee(body.emp_code)
    punched = ms_to_dt(body.punched_at) if body.punched_at is not None else now_whole_seconds()

    # Latest record that started at or before the punch-out. It is not filtered by date,
    # so an overnight shift started yesterday is found too. Records without a
    # punch_in (ABSENT / LEAVE) never match a date comparison.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_at = whole_seconds(record["punch_in"])
    problem = punch_gap_problem(punch_in_at, punched)
    if problem:
        bad_input("body", "punched_at", problem)

    derived = derive_fields(emp, record["date"], punch_in_at, punched)
    changes = {
        "punch_out": punched,
        "work_hours": derived["work_hours"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }
    # Compare-and-set: only update if the record is still open and unchanged.
    # Two simultaneous punch-outs -> one matches, the other gets None and a 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was already punched out or changed")
    return record_to_api(updated)


def attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> tuple[dict, int, int]:
    """Shared by GET /attendance and its explain, so both run the same query."""
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    start = check_day(date_from, "query", "date_from") if date_from else None
    end = check_day(date_to, "query", "date_to") if date_to else None
    if start and end and start > end:
        bad_input("query", "date_from", "date_from must not be after date_to")
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start.isoformat()
        if end:
            query["date"]["$lte"] = end.isoformat()
    if status:
        query["status"] = status
    return query, (page - 1) * page_size, page_size


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: DayParam = None,
    date_to: DayParam = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    query, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort(ATTENDANCE_SORT).skip(skip).limit(limit)
    return {
        "items": [record_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


STORED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[str, Path(pattern=DAY_PATTERN)], body: RegularizeRequest):
    day = check_day(date, "path", "date").isoformat()
    emp = find_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    new_status = body.status or record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            bad_input("body", "status", f"{new_status} clears the punch times; do not send them")
        new_in = new_out = None
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        if body.punch_in is not None:
            new_in = ms_to_dt(body.punch_in)
        else:
            new_in = whole_seconds(record["punch_in"]) if record.get("punch_in") else None
        if new_in is None:
            bad_input("body", "punch_in", "a presence status needs a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            bad_input("body", "punch_in", "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            new_out = ms_to_dt(body.punch_out)
        else:
            new_out = whole_seconds(record["punch_out"]) if record.get("punch_out") else None
        if new_out is not None:
            problem = punch_gap_problem(new_in, new_out)
            if problem:
                bad_input("body", "punch_out", problem)
        derived = derive_fields(emp, day, new_in, new_out)

    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_values = {
        "status": record["status"],
        "punch_in": whole_seconds(record["punch_in"]) if record.get("punch_in") else None,
        "punch_out": whole_seconds(record["punch_out"]) if record.get("punch_out") else None,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }

    # "A request that changes nothing" is judged on what the client sent. Derived
    # fields are recomputed after a real change and logged if they moved.
    if all(new_values[f] == old_values[f] for f in ("status", "punch_in", "punch_out")):
        bad_input("body", "status", "the request does not change anything")

    changes = {
        f: {"from": old_values[f], "to": new_values[f]}
        for f in STORED_FIELDS
        if new_values[f] != old_values[f]
    }
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic lock: the update only applies if the record still looks the way we read
    # it (same status, punch times and number of history entries). If another request
    # got there first we answer 409 instead of silently overwriting its history entry.
    guard: dict[str, Any] = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    history = record.get("history")
    guard["history"] = {"$size": len(history)} if isinstance(history, list) else {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": {f: new_values[f] for f in STORED_FIELDS}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was changed by someone else, please retry")
    return record_to_api(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics: pipeline builders
# --------------------------------------------------------------------------- #
# Small expression helpers. `prefix` lets the same logic work on a log document
# ("$status") and on a log nested after an $unwind ("$logs.status").
ZERO = Decimal128("0")


def f(name: str, prefix: str = "") -> str:
    return f"${prefix}{name}"


def is_present(prefix: str = "") -> dict:
    return {"$in": [f("status", prefix), list(PRESENCE)]}


def is_weekday(prefix: str = "") -> dict:
    # The date string is parsed as UTC midnight, so its weekday is the calendar weekday.
    parsed = {"$dateFromString": {"dateString": f("date", prefix), "format": "%Y-%m-%d"}}
    return {"$lte": [{"$isoDayOfWeek": parsed}, 5]}


def present_weight(prefix: str = "", weekdays_only: bool = True) -> dict:
    """1 for a present day, 0.5 for a half day, 0 when not present (R5, R6, R7)."""
    present = is_present(prefix)
    if weekdays_only:
        present = {"$and": [present, is_weekday(prefix)]}
    weight = {"$cond": [{"$eq": [{"$ifNull": [f("half_day", prefix), False]}, True]}, 0.5, 1]}
    return {"$cond": [present, weight, 0]}


def late_minutes_expr(prefix: str = "") -> dict:
    return {"$ifNull": [f("late_minutes", prefix), 0]}


def late_flag(prefix: str = "") -> dict:
    return {"$cond": [{"$gt": [late_minutes_expr(prefix), 0]}, 1, 0]}


def half_up_expr(expr: Any, places: int) -> dict:
    """Round half up inside the pipeline. MongoDB's own $round is banker's rounding, so
    we do floor(x * 10^places + 0.5) / 10^places on Decimal128 values (no float noise).
    A null input stays null."""
    scale = Decimal128(str(10 ** places))
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}}, scale]}


def monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_weight()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": late_flag()},
                "total_late_minutes": {"$sum": late_minutes_expr()},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = (d.isoformat() for d in month_bounds(month))
    match: dict[str, Any] = {"joined_on": {"$lte": last}}  # R9 headcount rule
    if department:
        match["department"] = department

    has_hours = {"$and": [is_present(), {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_weight()},
                "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, ZERO]}},
                "wh_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                "late_count": {"$sum": late_flag()},
                "total_late_minutes": {"$sum": late_minutes_expr()},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    return [
        # Start from employees, so people with zero logs are still counted.
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": per_employee,
                "as": "stats",
            }
        },
        {"$set": {"s": {"$first": "$stats"}}},  # no logs -> s is missing -> treated as 0
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
                "wh_sum": {"$sum": {"$ifNull": ["$s.wh_sum", ZERO]}},
                "wh_count": {"$sum": {"$ifNull": ["$s.wh_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$toDouble": half_up_expr({"$toDecimal": "$present_days"}, 2)},
                # mean over records: total hours / number of records (not mean of means)
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_count", 0]},
                        {"$toDouble": half_up_expr({"$divide": ["$wh_sum", "$wh_count"]}, 2)},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = (d.isoformat() for d in month_bounds(month))
    stages: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": late_minutes_expr()},
                "late_count": {"$sum": late_flag()},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        # Filter before ranking: ranks are computed inside the department only.
        stages.append({"$match": {"emp.department": department}})
    stages += [
        # $rank is "standard competition ranking": ties share a rank, next rank skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit is applied to the rank, not to the row position, so ties at the cutoff stay.
        {"$match": {"rank": {"$lte": limit}}},
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
    return stages


def trend_range(start_text: str, end_text: str) -> tuple[date, date]:
    start = check_day(start_text, "query", "from")
    end = check_day(end_text, "query", "to")
    if end < start:
        bad_input("query", "to", "to must not be before from")
    if (end - start).days + 1 > 92:
        bad_input("query", "to", "the range may not exceed 92 days")
    return start, end


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    first, last = start.isoformat(), end.isoformat()
    day_count = (end - start).days + 1
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)

    # One zero-filled row per calendar day of the range, generated inside MongoDB.
    # (employees is only used as a one-document source for $range.)
    one_row_per_day = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "offset": {"$range": [0, day_count]}}},
        {"$unwind": "$offset"},
        {
            "$replaceWith": {
                "_id": {
                    "$dateToString": {
                        "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$offset"}},
                        "format": "%Y-%m-%d",
                    }
                },
                "present": 0,
                "late": 0,
            }
        },
    ]
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": "$logs.date",
                "present": {"$sum": present_weight("logs.", weekdays_only=False)},
                "late": {"$sum": late_flag("logs.")},
            }
        },
        # Add the zero rows, then merge rows that share a date by summing them.
        {"$unionWith": {"coll": "employees", "pipeline": one_row_per_day}},
        {"$group": {"_id": "$_id", "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$sort": {"_id": 1}},
        # R9: headcount that day = department employees who had joined by then.
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$_id"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]},
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}},
                        5,
                    ]
                },
            }
        },
        {
            "$set": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up_expr({"$divide": [{"$toDecimal": "$present"}, "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # Mean of the (rounded, reported) rates over this row and up to 6 rows before it,
        # inside the requested range only. Null rates are skipped by $avg.
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$toDouble": "$present"},
                "late_count": "$late",
                "attendance_rate": {"$toDouble": "$rate"},
                "moving_avg_7d": {"$toDouble": half_up_expr("$moving", 4)},
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# 7b. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    emp = find_employee(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    stats = rows[0] if rows else {}

    # R7: working days start at joined_on for someone who joined during the month.
    joined = check_day(emp["joined_on"], "stored", "joined_on")
    working_days = count_weekdays(max(first, joined), last)
    present_days = Decimal(str(stats.get("present_days", 0)))
    pct = None
    if working_days > 0:
        pct = float(half_up(present_days / Decimal(working_days) * 100, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(half_up(present_days, 2)),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthParam,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DAY_PATTERN)],
    to: Annotated[str, Query(pattern=DAY_PATTERN)],
):
    start, end = trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def need(value: Any, name: str):
    if value is None or value == "":
        bad_input("query", name, "field required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: DayParam = None,
    date_to: DayParam = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DAY_PATTERN)] = None,
    to: DayParam = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    # Each branch builds the query with the very same function the real endpoint uses.
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        query, skip, size = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        command: dict[str, Any] = {
            "find": collection,
            "filter": query,
            "sort": dict(ATTENDANCE_SORT),
            "skip": skip,
            "limit": size,
        }
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            first, last = month_bounds(need(month, "month"))
            pipeline = monthly_pipeline(need(emp_code, "emp_code"), first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = department_summary_pipeline(need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(need(month, "month"), limit, department)
        else:  # department_trend
            collection = "employees"
            start, end = trend_range(need(from_, "from"), need(to, "to"))
            pipeline = department_trend_pipeline(need(department, "department"), start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # Turn BSON types (dates, Int64, ...) into plain JSON.
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
