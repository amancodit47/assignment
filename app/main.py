"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

All code lives in this single file as required by the assignment.
"""

import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# Epoch millis validation bounds (per OpenAPI schema)
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #

@app.on_event("startup")
def create_indexes():
    """Create all required indexes at startup, idempotently."""
    # Unique index on emp_code for employees - enforces concurrent duplicate creates
    db.employees.create_index("emp_code", unique=True, name="idx_emp_code_unique")

    # Unique compound index on (emp_code, date) for attendance_logs
    # Enforces one record per employee per day, prevents concurrent duplicate punch-ins
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_empcode_date_unique",
    )

    # Index for attendance_logs sorted by date desc, emp_code asc (for list_attendance)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_empcode_asc",
    )

    # Index for analytics queries that filter by date range
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_date_asc_empcode_asc",
    )

    # Index on employees.department for department queries
    db.employees.create_index("department", name="idx_department")

    # Index on employees.joined_on for headcount calculations
    db.employees.create_index("joined_on", name="idx_joined_on")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def round_half_up(value: float, decimals: int = 2) -> float:
    """Round using half-up rounding (not banker's rounding)."""
    d = Decimal(str(value))
    if decimals == 2:
        result = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    elif decimals == 4:
        result = d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    else:
        result = d.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP)
    return float(result)


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime, truncated to seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return truncate_to_seconds(dt)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds, or None if dt is None."""
    if dt is None:
        return None
    # Ensure timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)




def determine_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date per R1.
    For overnight shifts (shift_end <= shift_start): if punch_in IST time is before shift_end,
    the record belongs to the previous day.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if is_overnight:
        punch_time_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute
        end_minutes = eh * 60 + em
        if punch_time_minutes < end_minutes:
            # Belongs to previous day's shift
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes. Late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = floor(minutes since shift_start).
    Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    punch_in_ist = punch_in_utc.astimezone(IST) if punch_in_utc.tzinfo else punch_in_utc.replace(tzinfo=timezone.utc).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    # Build the shift start datetime on the same IST date as punch_in
    start = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)

    # For overnight shifts where punch_in is before midnight but shift_start is too,
    # this should still work since we're on the same date
    diff_seconds = (punch_in_ist - start).total_seconds()

    # Grace period is 10 minutes (600 seconds). Late only if strictly > 600 seconds
    if diff_seconds > 600:
        return int(diff_seconds // 60)  # floor of total minutes since shift_start
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)




def compute_overtime_with_shift(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: overtime_minutes = floor minutes between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    Handles overnight shifts properly.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    att_date = date_type.fromisoformat(attendance_date)

    if is_overnight:
        # For overnight shifts, shift_end is on the next calendar day
        next_day = att_date + timedelta(days=1)
        end_dt = datetime(next_day.year, next_day.month, next_day.day, eh, em, 0, tzinfo=IST)
    else:
        end_dt = datetime(att_date.year, att_date.month, att_date.day, eh, em, 0, tzinfo=IST)

    punch_out_ist = punch_out_utc.astimezone(IST) if punch_out_utc.tzinfo else punch_out_utc.replace(tzinfo=timezone.utc).astimezone(IST)

    diff_seconds = (punch_out_ist - end_dt).total_seconds()
    if diff_seconds < 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = true when the rounded work_hours < 4.50."""
    return work_hours < 4.50


def format_attendance_record(doc: dict) -> dict:
    """Format an attendance_logs document for the API response."""
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }

    # Format history entries
    for entry in doc.get("history", []):
        h = {
            "at": datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                h["changes"][field] = {
                    "from": datetime_to_epoch_ms(change.get("from")),
                    "to": datetime_to_epoch_ms(change.get("to")),
                }
            else:
                h["changes"][field] = {"from": change.get("from"), "to": change.get("to")}
        result["history"].append(h)

    return result


def format_employee(doc: dict) -> dict:
    """Format an employee document for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def parse_month(month: str) -> tuple:
    """Parse a YYYY-MM string and return (year, month_int, first_day_str, last_day_str)."""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(422, detail="month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, mon)
    first = f"{year:04d}-{mon:02d}-01"
    last = f"{year:04d}-{mon:02d}-{last_day:02d}"
    return year, mon, first, last


def count_working_days(start_date: str, end_date: str) -> int:
    """Count Mon-Fri days between two dates inclusive."""
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date)
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0 to Fri=4
            count += 1
        current += timedelta(days=1)
    return count


def get_month_working_days_for_employee(joined_on: str, month_first: str, month_last: str) -> int:
    """
    R7: Working days for an employee in a month.
    For an employee who joined mid-month, count from joined_on.
    """
    joined = date_type.fromisoformat(joined_on)
    first = date_type.fromisoformat(month_first)
    last = date_type.fromisoformat(month_last)

    # If joined after the month ends, 0 working days
    if joined > last:
        return 0

    # Effective start is the later of joined_on and month_first
    effective_start = max(joined, first)
    return count_working_days(effective_start.isoformat(), last.isoformat())


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 120 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email format")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def validate_shift_different(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if isinstance(v, float):
                raise ValueError("punched_at must be an integer, not a float")
            if v < EPOCH_MS_MIN:
                raise ValueError("punched_at looks like seconds, not milliseconds")
            if v > EPOCH_MS_MAX:
                raise ValueError("punched_at is out of range")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if isinstance(v, float):
                raise ValueError("punched_at must be an integer, not a float")
            if v < EPOCH_MS_MIN:
                raise ValueError("punched_at looks like seconds, not milliseconds")
            if v > EPOCH_MS_MAX:
                raise ValueError("punched_at is out of range")
        return v


class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if not v or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("status must be PRESENT, WFH, ON_DUTY, ABSENT, or LEAVE")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_ms_fields(cls, v):
        if v is not None:
            if isinstance(v, float):
                raise ValueError("value must be an integer, not a float")
            if v < EPOCH_MS_MIN:
                raise ValueError("value looks like seconds, not milliseconds")
            if v > EPOCH_MS_MAX:
                raise ValueError("value is out of range")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB is not available")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee. emp_code must be unique (enforced by DB index)."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Total counts filtered set."""
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size  # Fixed: was page * page_size (off by one)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(doc) for doc in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """Record a punch-in. Creates the day's attendance record."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Parse timestamp
    if body.punched_at:
        ts = epoch_ms_to_datetime(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # Determine attendance date per R1
    ts_ist = ts.astimezone(IST)
    attendance_date = determine_attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])

    # Compute late_minutes (R2)
    late = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    doc.pop("_id", None)
    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """Record a punch-out. Closes the most recent open record."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Parse timestamp
    if body.punched_at:
        ts = epoch_ms_to_datetime(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record with punch_in <= punched_at and no punch_out
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, detail="No open punch-in found for this employee")

    # Check if already punched out (for concurrent punch-out handling)
    # The find above already filters for punch_out=None, so if we get here, it's open.
    # But we need to use an atomic update to handle concurrency.

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punch_out must be after punch_in
    if ts <= punch_in_dt:
        raise HTTPException(422, detail="punch_out must be after punch_in")

    # Validate: punch_out must be within 24 hours of punch_in
    if (ts - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    work_hrs = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime_with_shift(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half = compute_half_day(work_hrs)

    # Atomic update: only update if punch_out is still None (concurrency)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hrs,
                "overtime_minutes": overtime,
                "half_day": half,
            }
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Record already punched out")

    # Fetch and return the updated record
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated is None:
        raise HTTPException(500, detail="Failed to fetch updated record")
    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc."""
    # Validate status
    if status is not None and status not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
        raise HTTPException(422, detail="Invalid status")

    # Validate date_from > date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [format_attendance_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """Regularize (manually correct) one day's record."""
    # Validate employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Validate date format
    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, detail="Invalid date format, expected YYYY-MM-DD")

    # Find the existing record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record found for this employee and date")

    # Determine the new status
    new_status = body.status if body.status is not None else record["status"]
    is_absence = new_status in ("ABSENT", "LEAVE")

    # Validation: ABSENT/LEAVE + punch times = 422
    if is_absence and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, detail="Cannot set punch times with ABSENT or LEAVE status")

    # Determine new punch times
    if is_absence:
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else record.get("punch_out")

        # Presence status requires a punch_in
        if new_punch_in is None:
            raise HTTPException(422, detail="Presence status requires a punch_in")

        # Ensure tz-aware (new_punch_in is guaranteed non-None past this point)
        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # Validate punch_in stays on the record's attendance date (R1)
        punch_in_ist = new_punch_in.astimezone(IST)
        expected_date = determine_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
        if expected_date != date:
            raise HTTPException(422, detail="punch_in must be on the record's attendance date")

        # Validate punch_out > punch_in and within 24 hours
        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Compute new derived fields
    if is_absence:
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        assert new_punch_in is not None  # guaranteed by raise on line 679
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"])
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime_with_shift(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # Build changes dict - only fields that actually changed
    changes = {}
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_overtime = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    # Compare punch times (handle None and tz-naive)
    def _normalize_dt(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    old_pi_norm = _normalize_dt(old_punch_in)
    new_pi_norm = _normalize_dt(new_punch_in)
    if old_pi_norm != new_pi_norm:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

    old_po_norm = _normalize_dt(old_punch_out)
    new_po_norm = _normalize_dt(new_punch_out)
    if old_po_norm != new_po_norm:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_overtime != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(422, detail="No fields were changed")

    # Build the history entry
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with optimistic concurrency on history length
    old_history_len = len(record.get("history", []))
    update = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late,
            "overtime_minutes": new_overtime,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    # Use history array length for optimistic concurrency control
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "$or": [
                {"history": {"$size": old_history_len}},
                {"history": {"$exists": False}},
            ],
        },
        update,
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Concurrent edit detected, please retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated is None:
        raise HTTPException(500, detail="Failed to fetch updated record")
    return format_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    year, mon, month_first, month_last = parse_month(month)

    # Working days for this employee
    working_days = get_month_working_days_for_employee(emp["joined_on"], month_first, month_last)

    # MongoDB aggregation pipeline
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_first, "$lte": month_last}}},
        {
            "$group": {
                "_id": None,
                "records": {"$push": "$$ROOT"},
            }
        },
    ]

    cursor = list(db.attendance_logs.aggregate(pipeline))

    if not cursor:
        records = []
    else:
        records = cursor[0]["records"]

    # Calculate metrics from stored values
    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for r in records:
        status = r.get("status")
        is_present_status = status in ("PRESENT", "WFH", "ON_DUTY")
        r_date = date_type.fromisoformat(r["date"])
        is_weekday = r_date.weekday() < 5

        if is_present_status and is_weekday:
            if r.get("half_day", False):
                present_days += 0.5
            else:
                present_days += 1.0

        if status == "LEAVE":
            leave_days += 1

        late_mins = r.get("late_minutes", 0)
        if late_mins > 0:
            late_count += 1
            total_late_minutes += late_mins

        total_overtime_minutes += r.get("overtime_minutes", 0)

    # Attendance percentage
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month."""
    year, mon, month_first, month_last = parse_month(month)

    # Get all employees (optionally filtered by department)
    emp_query = {}
    if department:
        emp_query["department"] = department

    # R9: headcount = employees whose joined_on <= last day of the month
    emp_query["joined_on"] = {"$lte": month_last}
    employees = list(db.employees.find(emp_query, {"_id": 0}))

    # Group employees by department
    dept_employees = {}
    for emp in employees:
        dept = emp["department"]
        if dept not in dept_employees:
            dept_employees[dept] = []
        dept_employees[dept].append(emp)

    # Get all emp_codes we care about
    emp_codes = [e["emp_code"] for e in employees]

    # Get attendance logs for the month for these employees
    log_query = {
        "emp_code": {"$in": emp_codes},
        "date": {"$gte": month_first, "$lte": month_last},
    }

    # Use aggregation to join with employees
    pipeline = [
        {"$match": log_query},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": {"path": "$employee", "preserveNullAndEmptyArrays": False}},
        {
            "$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                                            [2, 3, 4, 5, 6],  # Mon=2 to Fri=6 in $dayOfWeek
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "avg_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                    {"$gt": [{"$type": "$work_hours"}, "null"]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "avg_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                    {"$gt": [{"$type": "$work_hours"}, "null"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            "$late_minutes",
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    agg_results = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    items = []
    for dept in sorted(dept_employees.keys()):
        headcount = len(dept_employees[dept])
        if headcount == 0:
            continue

        agg = agg_results.get(dept, {})

        present_days = agg.get("present_days", 0)
        wh_sum = agg.get("avg_work_hours_sum", 0)
        wh_count = agg.get("avg_work_hours_count", 0)
        avg_wh = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": round_half_up(present_days, 2),
            "avg_work_hours": avg_wh,
            "late_count": agg.get("late_count", 0),
            "total_late_minutes": agg.get("total_late_minutes", 0),
            "leave_count": agg.get("leave_count", 0),
            "on_duty_count": agg.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month with standard competition ranking."""
    _, _, month_first, month_last = parse_month(month)

    # Build pipeline
    match_stage = {"date": {"$gte": month_first, "$lte": month_last}, "late_minutes": {"$gt": 0}}

    pipeline = [
        {"$match": match_stage},
        # Join with employees to get name, department, and to filter invalid emp_codes
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Filter by department if specified
    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    # Apply standard competition ranking
    # Ties share a rank, next rank is skipped
    ranked = []
    if results:
        current_rank = 1
        for i, r in enumerate(results):
            if i > 0 and r["total_late_minutes"] < results[i - 1]["total_late_minutes"]:
                current_rank = i + 1
            ranked.append({
                "rank": current_rank,
                "emp_code": r["_id"],
                "name": r["name"],
                "department": r["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            })

    # Filter: return every employee whose rank <= limit
    filtered = [r for r in ranked if r["rank"] <= limit]

    return {"month": month, "items": filtered}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """Daily attendance trend for a department using MongoDB aggregation with $densify."""
    # Validate department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # Validate dates
    try:
        from_date = date_type.fromisoformat(date_from)
        to_date = date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, detail="Invalid date format")

    if to_date < from_date:
        raise HTTPException(422, detail="'to' must not be before 'from'")

    if (to_date - from_date).days > 92:
        raise HTTPException(422, detail="Date range must not exceed 92 days")

    # Get all employees in this department
    all_dept_employees = list(
        db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    )
    dept_emp_codes = [e["emp_code"] for e in all_dept_employees]

    # Convert date range boundaries to BSON datetime for $densify
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc)

    # MongoDB aggregation pipeline using $densify for gap-filling
    pipeline = [
        # Match attendance logs in range for this department's employees
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        # Convert string date to BSON date for $densify
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            }
        },
        # Group by date: compute present_count and late_count per day
        {
            "$group": {
                "_id": "$date_dt",
                "date_str": {"$first": "$date"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        # Rename _id to date_dt for $densify
        {"$addFields": {"date_dt": "$_id"}},
        # $densify: fill gaps for every calendar day in the range
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + timedelta(days=1)],
                },
            }
        },
        # Fill nulls for densified (gap) rows
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {
                    "$ifNull": [
                        "$date_str",
                        {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                    ]
                },
            }
        },
        # Sort by date ascending
        {"$sort": {"date_dt": ASCENDING}},
    ]

    agg_results = list(db.attendance_logs.aggregate(pipeline))

    # Build response items with headcount and attendance_rate computed per-day
    # (headcount depends on employee joined_on which varies per day — R9)
    items = []
    rate_window = []

    for row in agg_results:
        day_str = row["date_str"]
        day_dt = row["date_dt"]

        # Determine day of week (0=Monday in Python, but date_dt is a BSON datetime)
        if isinstance(day_dt, datetime):
            is_working_day = day_dt.weekday() < 5
        else:
            is_working_day = date_type.fromisoformat(day_str).weekday() < 5

        # Headcount: employees with joined_on <= this day (R9)
        headcount = sum(1 for e in all_dept_employees if e["joined_on"] <= day_str)

        present_count = row["present_count"]
        late_count = row["late_count"]

        # Attendance rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        rate_window.append(attendance_rate)

        # Moving average: mean of non-null rates in the last 7 entries
        window = rate_window[-7:]
        non_null_rates = [r for r in window if r is not None]
        if non_null_rates:
            moving_avg = round_half_up(sum(non_null_rates) / len(non_null_rates), 4)
        else:
            moving_avg = None

        items.append({
            "date": day_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": round_half_up(present_count, 2),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, alias="date_from"),
    date_to: Optional[str] = Query(default=None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Run MongoDB explain for an endpoint's main query."""
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size

        explain = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        _, _, month_first, month_last = parse_month(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_first, "$lte": month_last}}},
            {"$group": {"_id": None, "records": {"$push": "$$ROOT"}}},
        ]
        explain = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, month_first, month_last = parse_month(month)
        emp_q: dict = {"joined_on": {"$lte": month_last}}
        if department:
            emp_q["department"] = department
        emp_codes = [e["emp_code"] for e in db.employees.find(emp_q, {"emp_code": 1})]
        log_match = {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": month_first, "$lte": month_last},
        }
        pipeline = [
            {"$match": log_match},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {
                "$group": {
                    "_id": "$employee.department",
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }
            },
        ]
        explain = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, month_first, month_last = parse_month(month)
        match_stage = {"date": {"$gte": month_first, "$lte": month_last}, "late_minutes": {"$gt": 0}}
        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        ])
        explain = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required")
        if not from_date or not to_date:
            raise HTTPException(422, detail="from and to are required")
        dept_emp_codes = [
            e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})
        ]
        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }
            },
            {"$sort": {"_id": ASCENDING}},
        ]
        explain = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
