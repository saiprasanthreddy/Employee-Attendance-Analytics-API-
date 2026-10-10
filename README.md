# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# Create a local .env file first; see the MongoDB Atlas configuration below.
python -m uvicorn app.main:app --port 8000
```

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# Create a local .env file first; see the MongoDB Atlas configuration below.
python -m uvicorn app.main:app --port 8000
```

Needs MongoDB 6.0 or newer (aggregation uses `$setWindowFields`, `$dateAdd`, `$unionWith`). Indexes are created at startup and the app needs no manual steps. Do not commit `.env`; it is already covered by `.gitignore`.

## MongoDB Atlas configuration

Create a local `.env` file in the repository root. It is intentionally ignored by Git and must never be committed.

```env
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

In MongoDB Atlas, allow your current IP address under **Network Access** and ensure the database user has read/write access. The app creates the `employees` and `attendance_logs` collections after data is added. The configured local Atlas connection was verified with `GET /health`; its secret connection string is not stored in this repository.

## Features

- Create and list employees.
- Record punch-in and punch-out events with IST, overnight-shift, late-arrival, overtime, half-day, and work-hour rules.
- Regularize attendance while preserving an append-only audit history.
- Produce employee monthly summaries, department summaries, late-comer rankings, and daily department trends with MongoDB aggregation pipelines.
- Create indexes automatically for data integrity and query performance.

## API overview

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Confirms MongoDB is reachable. |
| `POST` / `GET /employees` | Creates or lists employees. |
| `POST /attendance/punch-in` | Starts an attendance record. |
| `POST /attendance/punch-out` | Completes the latest open record. |
| `GET /attendance` | Lists filtered and paginated attendance records. |
| `PATCH /attendance/{emp_code}/{date}` | Corrects a record and appends audit history. |
| `GET /analytics/...` | Returns employee, department, leaderboard, and trend analytics. |
| `GET /admin/explain/{endpoint}` | Shows MongoDB query-plan information. |

If an existing `.venv` reports that its Python executable cannot be found, remove that stale environment and recreate it with an installed Python 3.11+ interpreter:

```powershell
Remove-Item -Recurse -Force .venv
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

The server root (`http://localhost:8000/`) returns a status payload. Use `/health` to verify MongoDB connectivity and `/docs` for the interactive API docs.

## Tests
```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_rules.py     # rules R1-R5, validation, pipeline construction; no database needed
python tests/smoke.py                    # needs MongoDB + the running API; seeds its own EMP90xx / ZZ-Smoke-* data
```

## What is done
Every endpoint in `openapi.yaml`, including the explain endpoint. `REVIEW.md` and `DECISIONS.md` are filled in.

## Choices where the contract left room
- Trend range limit: 92 days counts both end dates, so `from` to `to` may span at most 92 calendar days (93 is 422).
- `moving_avg_7d` averages the reported (4-decimal, half-up) `attendance_rate` values.
- A PATCH "changes nothing" when `status`, `punch_in` and `punch_out` all equal the stored values. Derived fields are recomputed and logged only after a real change.
- Concurrent PATCHes use an optimistic check; the loser gets 409.
- Logs whose date is before the employee's `joined_on` are still counted in analytics (the contract does not say to drop them).
