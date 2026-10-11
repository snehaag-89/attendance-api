# Employee Attendance & Analytics API

A small attendance API built with Python, FastAPI and MongoDB: punch-in and punch-out with business rules, manual corrections with an audit trail, and analytics endpoints built on MongoDB aggregation pipelines. All code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (local, Docker or MongoDB Atlas)

## Run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

python sample_seed.py              # optional: loads the sample data
uvicorn app.main:app --port 8000
```

On Windows PowerShell use `$env:MONGO_URI="..."` instead of `export`. You can also put `MONGO_URI` and `MONGO_DB` in a local `.env` file (it is not committed); real environment variables always win over `.env`.

Open `http://localhost:8000/docs` for the interactive API docs. Indexes are created automatically at startup.

## Implemented

- `GET /health` (pings MongoDB)
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` (manual correction, derived fields recomputed, history appended)
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}` (executionStats of the real query or pipeline)

## Notes / what is missing

- Tested by hand against the sample data on MongoDB Atlas: every endpoint, and `/admin/explain` shows `IXSCAN` and no `COLLSCAN` for all five queries.
- Race tests with 20 parallel requests gave exactly one success for create-employee, punch-in and punch-out; the rest got 409.
- Load-tested with about 105,000 attendance records (550 employees) on MongoDB Atlas: every endpoint answered in roughly 120-220 ms, and /admin/explain shows IXSCAN and no COLLSCAN for all five queries.
- Review of the starter code: `REVIEW.md`. Design answers: `DECISIONS.md`.