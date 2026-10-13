# Employee Attendance & Analytics API

A FastAPI-based REST API for managing employee attendance with punch-in/out, manual corrections (regularizations) with an audit trail, and analytics endpoints powered by MongoDB aggregation pipelines.

## Features

- **Employee Management**: Create and list employees with pagination and filtering
- **Attendance Tracking**: Punch-in and punch-out with business rules for late detection, overtime, and half-day computation
- **Manual Corrections**: Regularize attendance records with a full audit trail (`history` array)
- **Analytics**:
  - Monthly employee attendance summary (working days, present days, leave, late stats)
  - Department summary with headcount, avg work hours, late/leave/on-duty counts
  - Late-comers leaderboard with standard competition ranking
  - Daily department attendance trend with 7-day moving average
- **Admin**: MongoDB `explain` endpoint for query plan inspection
- **Concurrency Safety**: Unique indexes prevent duplicate records; atomic updates prevent lost writes

## Tech Stack

- **Python 3.11+**
- **FastAPI** (with Pydantic v2 validation)
- **MongoDB 6.0+** (via PyMongo)
- **uvicorn** (ASGI server)

## Setup

```bash
# Create and activate virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
# source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure MongoDB (create a .env file)
echo MONGO_URI=mongodb://localhost:27017 > .env
echo MONGO_DB=attendance_db >> .env

# Seed sample data (optional)
python sample_seed.py

# Start the server
uvicorn app.main:app --port 8000 --reload
```

### MongoDB

You need MongoDB 6.0 or newer. Options:
- **Docker**: `docker run -p 27017:27017 mongo:7`
- **MongoDB Atlas**: Free M0 cluster works

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Readiness probe (pings MongoDB) |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees (paginated, filterable) |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | List attendance records (paginated, filterable) |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize (correct) a record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for an employee |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily department trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain output |

## Business Rules

All business rules (R1–R10) are implemented as specified in the OpenAPI contract:
- **R1**: IST timezone, overnight shift date handling
- **R2**: 10-minute grace period for lateness
- **R3**: Overtime only counted when ≥ 30 minutes past shift end
- **R4**: Work hours = (punch_out − punch_in) / 3600, 2-decimal half-up rounding
- **R5**: Half day when work_hours < 4.50
- **R6**: PRESENT, WFH, ON_DUTY count as present
- **R7**: Working days = Mon–Fri from joined_on
- **R8**: 2-decimal half-up rounding (rates: 4-decimal)
- **R9**: Headcount includes employees with zero logs if joined by month-end
- **R10**: Pagination starts at page 1, max page_size 100

## Project Structure

```
├── app/
│   ├── __init__.py
│   └── main.py          # All application code
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── sample_seed.py
├── requirements.txt
├── REVIEW.md            # Code review of starter
├── DECISIONS.md         # Design decisions
└── README.md            # This file
```

## What's Implemented

All endpoints from the OpenAPI contract are implemented, including:
- All CRUD and punch-in/out operations with full validation
- Regularization with audit trail and optimistic concurrency
- All four analytics endpoints with MongoDB aggregation pipelines
- The admin explain endpoint for all five target endpoints
- Proper indexes for performance (no COLLSCAN)
- Half-up rounding throughout (not banker's rounding)
- Epoch millisecond conversion at the API boundary
- Overnight shift handling for dates, late, and overtime
