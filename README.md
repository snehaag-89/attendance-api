# Employee Attendance & Analytics API

A small attendance backend built with **Python, FastAPI and MongoDB**. Employees punch in and out under business rules (lateness, overtime, half days, overnight shifts), managers correct records with an audit trail, and four analytics endpoints are built on MongoDB aggregation pipelines. All code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (local, Docker or Atlas).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"     # PowerShell: $env:MONGO_URI="..."
export MONGO_DB="attendance_db"

python sample_seed.py              # optional: load the sample data
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` can also go in a local `.env` file (not committed); real environment variables always win. Open `http://localhost:8000/docs` for the interactive docs. Indexes are created automatically at startup.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Pings MongoDB |
| POST / GET | `/employees` | Create (409 on duplicate) and list with pagination |
| POST | `/attendance/punch-in` | Punch in (409 if the date already has a record) |
| POST | `/attendance/punch-out` | Punch out, compute work hours, overtime, half day |
| GET | `/attendance` | List records with filters and pagination |
| PATCH | `/attendance/{emp_code}/{date}` | Manual correction; derived fields recomputed, change logged in `history` |
| GET | `/analytics/employees/{emp_code}/monthly` | One employee's month |
| GET | `/analytics/departments/summary` | Per-department month summary |
| GET | `/analytics/leaderboard/late` | Late ranking with ties (1, 2, 2, 4) |
| GET | `/analytics/departments/{department}/trend` | Daily trend with a 7-day moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB `executionStats` for the real query |

Times are epoch milliseconds, dates are IST (`YYYY-MM-DD`), and errors are `404`, `409` or `422`.

## How it works

- **Time zones:** MongoDB stores UTC (`tz_aware=True`); IST is used only to decide dates and shift boundaries. An overnight shift belongs to the day it started.
- **Rounding:** half-up everywhere, using integer arithmetic or `Decimal`, because Python's `round()` and MongoDB's `$round` round half to even.
- **Concurrency:** no check-then-insert. Unique indexes on `emp_code` and on (`emp_code`, `date`) let MongoDB accept exactly one of several simultaneous inserts (the rest get 409). Punch-out updates only a record whose `punch_out` is still `null`, and PATCH uses an optimistic lock on `history`.
- **Indexes:** the unique ones above, plus (`date` desc, `emp_code`), (`emp_code`, `punch_in` desc), (`department`, `joined_on`) and (`joined_on`).
- **Analytics:** department summary starts from `employees` so people without logs still count in headcount; the leaderboard uses `$rank`; the trend uses `$densify` and `$setWindowFields`.

## Testing

Checked by hand against the sample data on MongoDB Atlas, with analytics values compared to an independent reference. 20 parallel requests each for create-employee, punch-in and punch-out gave exactly one success. Load-tested with about 105,000 attendance records: every endpoint answered in roughly 120 to 220 ms, and `/admin/explain` shows `IXSCAN` and no `COLLSCAN` for all five queries.

## Notes

- Everything in the endpoint list is implemented. There is no automated test suite in the repo and no authentication.
- The pipelines need MongoDB 6.0 or newer.
- Defects found in the starter code: `REVIEW.md`. Design answers: `DECISIONS.md`.