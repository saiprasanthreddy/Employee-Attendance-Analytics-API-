# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Set MONGO_URI and MONGO_DB in .env. Environment variables take precedence.
python -m uvicorn app.main:app --port 8000
```

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
# Set MONGO_URI and MONGO_DB in .env. Environment variables take precedence.
python -m uvicorn app.main:app --port 8000
```

Needs MongoDB 6.0 or newer (aggregation uses `$setWindowFields`, `$dateAdd`, `$unionWith`). Indexes are created at startup and the app needs no manual steps. Do not commit `.env`; it is already covered by `.gitignore`.

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
