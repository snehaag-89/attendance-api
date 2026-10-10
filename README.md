# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"            # or put MONGO_URI / MONGO_DB in a local .env
export MONGO_DB="attendance_db"
python sample_seed.py                                   # optional: load the sample data
uvicorn app.main:app --port 8000
```
Open `http://localhost:8000/docs`. Indexes are created at startup.

## Implemented
Health, employees (create/list), punch-in, punch-out, attendance list, PATCH correction with audit history, the four analytics endpoints (monthly summary, department summary, late leaderboard, department trend) and `/admin/explain/{endpoint}`.

## Notes / what is missing
- Add here anything you did not finish or could not test (for example: not load-tested with 100k records).
- Review of the starter code: `REVIEW.md`. Design answers: `DECISIONS.md`.
