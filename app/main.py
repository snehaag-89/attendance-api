"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override variables that are already set in the environment

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """create_index is idempotent: running it again with the same spec does nothing."""
    # one employee per emp_code (also makes concurrent duplicate creates safe)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    # department filters + "joined_on <= day" headcount queries
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    # same headcount query when no department is given
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # one record per (employee, attendance date): makes punch-in race-safe, and serves
    # per-employee date-range queries (PATCH lookup, monthly summary, list by emp_code)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    # list/leaderboard/summary queries that filter by date range without an emp_code
    # (sort order of GET /attendance: date desc, emp_code asc)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    # punch-out: "latest record of this employee with punch_in <= punched_at"
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """datetime -> epoch milliseconds (None stays None). Naive datetimes are treated as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_min(shift_end) <= hhmm_to_min(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in, except for overnight shifts, where a
    punch-in earlier than shift_end (IST) belongs to the shift that started the day before."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.hour * 60 + local.minute < hhmm_to_min(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """The (start, end) instants of the shift that belongs to attendance date `date_str`."""
    d = date_cls.fromisoformat(date_str)
    s, e = hhmm_to_min(shift_start), hhmm_to_min(shift_end)
    midnight = datetime(d.year, d.month, d.day, tzinfo=IST)
    start_dt = midnight + timedelta(minutes=s)
    end_dt = midnight + timedelta(minutes=e) + (timedelta(days=1) if e <= s else timedelta())
    return start_dt, end_dt


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    start_dt, _ = shift_bounds(date_str, shift_start, shift_end)
    secs = int((punch_in - start_dt).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, counted only when >= 30."""
    _, end_dt = shift_bounds(date_str, shift_start, shift_end)
    mins = int((punch_out - end_dt).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded to 2 decimals half-up. Pure integer maths, so no float surprises:
    hundredths = floor(secs * 100 / 3600 + 1/2) = (secs * 200 + 3600) // 7200"""
    secs = int((punch_out - punch_in).total_seconds())
    return ((secs * 200 + 3600) // 7200) / 100


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.5


def is_valid_date(s: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        date_cls.fromisoformat(s)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Serialisation (MongoDB document -> API JSON)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):  # dates nested inside 'changes' also become epoch ms
            changes[key] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[key] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Legacy documents may miss (or hold null in) late_minutes / overtime_minutes / half_day / history: use defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        if not is_valid_date(v):
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database not reachable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, date_str, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one of N racers wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in <= ts (works across midnight; ABSENT/LEAVE have no punch_in)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_dt = rec["punch_in"]
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_dt, ts)
    # filter on punch_out == null makes the update atomic: of two simultaneous punch-outs only one matches
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
                "half_day": compute_half_day(work_hours),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and (later) /admin/explain so both run exactly the same query."""
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def check_attendance_filters(date_from, date_to):
    for value in (date_from, date_to):
        if value is not None and not is_valid_date(value):
            raise HTTPException(422, "dates must be valid YYYY-MM-DD")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_attendance_filters(date_from, date_to)
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}



# --------------------------------------------------------------------------- #
# Manual correction (regularize)
# --------------------------------------------------------------------------- #
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # omitted = unchanged; an explicit null is not a valid value for these fields
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    if not is_valid_date(date):
        raise HTTPException(422, "date must be a valid YYYY-MM-DD date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status or rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")

    # 1) work out the final times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{new_status} clears punch times: do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, f"status {new_status} requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours of it")

    # 2) recompute every derived field from the final state (R2-R5)
    if new_status in ("ABSENT", "LEAVE"):
        late, work_hours, overtime, half_day = 0, None, 0, False
    else:
        late = compute_late_minutes(new_in, rec["date"], emp["shift_start"], emp["shift_end"])
        if new_out is not None:
            work_hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, rec["date"], emp["shift_start"], emp["shift_end"])
            half_day = compute_half_day(work_hours)
        else:
            work_hours, overtime, half_day = None, 0, False

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    old_values = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }

    # 3) history records only the fields that really changed
    changes = {k: {"from": old_values[k], "to": v} for k, v in new_values.items() if v != old_values[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # 4) optimistic concurrency: the update only applies if nobody changed the record since we read it.
    #    Every PATCH appends to history, so "history has no element at index n" means nobody else patched.
    n = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{n}": {"$exists": False},
            "punch_out": old_out,  # also protects against a concurrent punch-out
            "status": rec["status"],
        },
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_range(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31') as date strings, plus the two date objects."""
    if not re.fullmatch(MONTH_PATTERN, month) or int(month[:4]) < 1:
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    first = date_cls(year, mon, 1)
    last = (date_cls(year + 1, 1, 1) if mon == 12 else date_cls(year, mon + 1, 1)) - timedelta(days=1)
    return first, last


def count_working_days(first: date_cls, last: date_cls) -> int:
    """R7: Mon-Fri days in [first, last] inclusive."""
    if last < first:
        return 0
    days = (last - first).days + 1
    return sum(1 for i in range(days) if (first + timedelta(days=i)).weekday() < 5)


def half_up(value, places: int) -> float:
    """R8: half-up rounding done on an exact Decimal (MongoDB's $round is half-to-even, Python's round() too)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(value).quantize(q, rounding=ROUND_HALF_UP))


def weekday_expr(p: str) -> dict:
    """True when the record's date string falls on Mon-Fri ($dayOfWeek: 1 = Sunday ... 7 = Saturday)."""
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": p + "date"}}}, [2, 3, 4, 5, 6]]}


def present_value_expr(p: str, weekdays_only: bool) -> dict:
    """1 for a present-status record (0.5 if half_day), else 0. `p` is '$' for a top-level doc or '$$l.' inside $map."""
    is_present = {"$in": [p + "status", PRESENCE_STATUSES]}
    cond = {"$and": [is_present, weekday_expr(p)]} if weekdays_only else is_present
    return {"$cond": [cond, {"$cond": [{"$eq": [{"$ifNull": [p + "half_day", False]}, True]}, 0.5, 1]}, 0]}


def late_flag_expr(p: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [p + "late_minutes", 0]}, 0]}, 1, 0]}


def run_explain(collection: str, command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out))  # makes ObjectId/Timestamp/etc. JSON-safe


# --------------------------------------------------------------------------- #
# Analytics 1: employee monthly summary
# --------------------------------------------------------------------------- #
def monthly_pipeline(emp_code: str, first: date_cls, last: date_cls) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_value_expr("$", weekdays_only=True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": late_flag_expr("$")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_range(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}

    working_days = count_working_days(max(first, date_cls.fromisoformat(emp["joined_on"])), last)  # R7
    present_days = Decimal(str(agg.get("present_days", 0)))
    pct = half_up(present_days * 100 / working_days, 2) if working_days > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present_days, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics 2: department summary
# --------------------------------------------------------------------------- #
def dept_summary_pipeline(first: date_cls, last: date_cls, department: Optional[str]) -> list:
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: headcount = joined on/before month end
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [{"$in": ["$$l.status", PRESENCE_STATUSES]}, {"$isNumber": "$$l.work_hours"}]}
    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": present_value_expr("$$l.", True)}}},
                "late_count": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": late_flag_expr("$$l.")}}},
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
                "on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}},
                # work hours summed as integer hundredths so the average can be rounded exactly (half-up) in Python
                "wh_cents": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [has_hours, {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]},
                        }
                    }
                },
                "wh_n": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": has_hours}}},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "wh_cents": {"$sum": "$wh_cents"},
                "wh_n": {"$sum": "$wh_n"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def employees_hint(department: Optional[str]):
    """Without a department filter the planner might pick a collection scan on the tiny employees
    collection; hinting the joined_on index keeps the plan index-based and predictable."""
    return None if department is not None else {"joined_on": 1}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_range(month)
    kwargs = {}
    hint = employees_hint(department)
    if hint:
        kwargs["hint"] = hint
    items = []
    for r in db.employees.aggregate(dept_summary_pipeline(first, last, department), **kwargs):
        n = int(r["wh_n"])
        avg = half_up(Decimal(int(r["wh_cents"])) / (100 * n), 2) if n > 0 else None
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": half_up(Decimal(str(r["present_days"])), 2),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics 3: late leaderboard
# --------------------------------------------------------------------------- #
def leaderboard_pipeline(first: date_cls, last: date_cls, department: Optional[str], limit: int) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties can exceed it
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


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_range(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, last, department, limit))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics 4: department daily trend
# --------------------------------------------------------------------------- #
def parse_trend_range(date_from: str, date_to: str):
    if not (is_valid_date(date_from) and is_valid_date(date_to)):
        raise HTTPException(422, "from and to must be valid YYYY-MM-DD dates")
    d_from, d_to = date_cls.fromisoformat(date_from), date_cls.fromisoformat(date_to)
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


def load_department_employees(department: str):
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return [e["emp_code"] for e in emps], sorted(e["joined_on"] for e in emps)


def trend_pipeline(codes: list, joined_on: list, d_from: date_cls, d_to: date_cls) -> list:
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    window = {"documents": [-6, 0]}  # current row + up to 6 preceding rows, all inside the requested range
    return [
        # 1) per-day totals from the logs of this department's employees
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": present_value_expr("$", False)}, "late_count": {"$sum": late_flag_expr("$")}}},
        # 2) guarantee at least one row, so $densify still has something to fill when the range has no logs at all
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": d_from.isoformat(), "present_count": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        # 3) gap filling: one row for every calendar day in [from, to]
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # 4) R9 headcount: department employees whose joined_on <= this day
        {"$addFields": {"headcount": {"$size": {"$filter": {"input": joined_on, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        # 5) rate in integer ten-thousandths, rounded half-up with exact integer maths:
        #    floor(present/headcount * 10000 + 1/2) = floor((2*present*10000 + headcount) / (2*headcount))
        {
            "$addFields": {
                "rate_int": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": [{"$multiply": ["$present_count", 2]}, 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        # 6) 7-day moving average over the non-null rates (sum and count over the window)
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "win_sum": {"$sum": "$rate_int", "window": window},
                    "win_n": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]}, "window": window},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "rate_int": 1,
                "avg_int": {
                    "$cond": [
                        {"$gt": ["$win_n", 0]},
                        {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$win_sum"]}, "$win_n"]}, {"$multiply": [2, "$win_n"]}]}},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = parse_trend_range(from_, to)
    codes, joined_on = load_department_employees(department)
    items = []
    for r in db.attendance_logs.aggregate(trend_pipeline(codes, joined_on, d_from, d_to)):
        rate, avg = r.get("rate_int"), r.get("avg_int")
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": None if rate is None else int(rate) / 10000,
                "moving_avg_7d": None if avg is None else int(avg) / 10000,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**params):
        missing = [k for k, v in params.items() if v is None]
        if missing:
            raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")

    # Each branch builds the query with the same helpers the real endpoint uses.
    if endpoint == "attendance_list":
        check_attendance_filters(date_from, date_to)
        q = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        first, last = month_range(month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        first, last = month_range(month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": dept_summary_pipeline(first, last, department), "cursor": {}}
        hint = employees_hint(department)
        if hint:
            command["hint"] = hint
    elif endpoint == "late_leaderboard":
        need(month=month)
        first, last = month_range(month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": leaderboard_pipeline(first, last, department, limit), "cursor": {}}
    else:  # department_trend
        need(department=department, **{"from": from_, "to": to})
        d_from, d_to = parse_trend_range(from_, to)
        codes, joined_on = load_department_employees(department)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": trend_pipeline(codes, joined_on, d_from, d_to), "cursor": {}}

    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(collection, command)}
