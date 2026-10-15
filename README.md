# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, attendance punch-in/punch-out, manual attendance corrections with an audit trail, and monthly/department analytics.

## Stack

- Python 3.11+
- FastAPI and Pydantic
- PyMongo / MongoDB 6.0+
- Uvicorn

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure `MONGO_URI` and `MONGO_DB` as environment variables. For local development, a `.env` file may be used; never commit it or publish credentials.
4. Start the API from this repository root:

   ```bash
   uvicorn app.main:app --port 8000
   ```

5. Open `http://127.0.0.1:8000/docs` for interactive Swagger documentation. `GET /health` checks MongoDB readiness.

## Main endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` for audited manual corrections
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}` for query-plan diagnostics

## Key rules

The API uses IST attendance dates, supports overnight shifts, applies a 10-minute late grace period, counts overtime only from 30 minutes, rounds work hours to two decimals using half-up rounding, and treats half-days as 0.5 present days. Pagination starts at page 1 and limits page size to 100. Manual corrections append audit history.

See `openapi.yaml` for the endpoint contract and R1–R10 rules, and `DATA_MODEL.md` for MongoDB document shapes. `sample_seed.py` loads the sample dataset and **deletes existing documents in the configured collections first**, so use it only with a disposable development database.
