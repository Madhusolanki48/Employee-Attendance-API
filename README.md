# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# set MONGO_URI and MONGO_DB as environment variables (or in a local .env file)
uvicorn app.main:app --port 8000
```

- Needs MongoDB 6.0+ (uses `$densify` and `$setWindowFields`).
- Indexes are created automatically at startup.
- Interactive docs: http://localhost:8000/docs

## Notes

- Instants are epoch milliseconds in the API and BSON dates (UTC) in MongoDB. They are truncated to whole seconds.
- All analytics are MongoDB aggregation pipelines. Half-up rounding of final numbers is applied in Python, because MongoDB's `$round` uses banker's rounding.
- `GET /admin/explain/{endpoint}` uses the same pipeline builders as the real endpoints.
- See `REVIEW.md` for the starter-code review and `DECISIONS.md` for design choices.
