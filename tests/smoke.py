"""Integration smoke test. Needs MongoDB and the API running.

    uvicorn app.main:app --port 8000        # in one terminal
    python tests/smoke.py                   # in another   (BASE_URL defaults to http://localhost:8000)

It only touches its own data: employee codes EMP9001-EMP9005 / EMP9101 and departments
named "ZZ-Smoke-*". Those are deleted and re-created on every run.
The expected numbers below were worked out by hand from the rules R1 - R10.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.getenv("BASE_URL", "http://localhost:8000")
IST = timezone(timedelta(hours=5, minutes=30))
db = MongoClient(os.getenv("MONGO_URI") or "mongodb://localhost:27017", tz_aware=True)[
    os.getenv("MONGO_DB") or "attendance_db"
]

failures = 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def check(name, actual, expected):
    global failures
    ok = actual == expected
    failures += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n      expected {expected}\n      actual   {actual}"))


def ms(y, m, d, hh, mm, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=IST).timestamp() * 1000)


def when(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
CODES = [f"EMP90{n:02d}" for n in range(1, 6)] + ["EMP9101", "EMP9102"]
db.employees.delete_many({"emp_code": {"$in": CODES}})
db.attendance_logs.delete_many({"emp_code": {"$in": CODES}})


def emp(code, dept, joined):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code}@example.com", "department": dept,
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined, "created_at": datetime.now(timezone.utc)}


def log(code, day, status, wh=None, late=0, ot=0, half=False):
    present = status in ("PRESENT", "WFH", "ON_DUTY")
    y, m, d = (int(x) for x in day.split("-"))
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": when(y, m, d, 9, 30) if present else None,
            "punch_out": when(y, m, d, 18, 30) if present and wh is not None else None,
            "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}


db.employees.insert_many([
    emp("EMP9001", "ZZ-Smoke-A", "2026-01-05"),
    emp("EMP9002", "ZZ-Smoke-A", "2026-01-05"),
    emp("EMP9003", "ZZ-Smoke-A", "2026-01-05"),
    emp("EMP9004", "ZZ-Smoke-B", "2026-07-13"),  # joins mid-month
    emp("EMP9005", "ZZ-Smoke-B", "2026-07-15"),  # never has a log
])
db.attendance_logs.insert_many([
    log("EMP9001", "2026-07-06", "PRESENT", 9.0),
    log("EMP9001", "2026-07-07", "PRESENT", 8.0, late=45),
    log("EMP9001", "2026-07-08", "WFH", 3.5, half=True),
    log("EMP9001", "2026-07-09", "LEAVE"),
    log("EMP9001", "2026-07-11", "PRESENT", 5.0, late=20, ot=40),      # Saturday
    log("EMP9002", "2026-07-06", "PRESENT", 8.5, late=45),
    log("EMP9002", "2026-07-07", "ON_DUTY", 7.25),
    log("EMP9002", "2026-07-08", "ABSENT"),
    log("EMP9003", "2026-07-06", "PRESENT", 8.0, late=45),             # ties with EMP9002
    log("EMP9004", "2026-07-13", "PRESENT", 4.0, half=True),
    log("EMP9004", "2026-07-14", "PRESENT", 9.0, late=10),
])

# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
print("\n== employee monthly")
_, r = call("GET", "/analytics/employees/EMP9001/monthly?month=2026-07")
check("EMP9001 monthly", r, {"emp_code": "EMP9001", "month": "2026-07", "working_days": 23, "present_days": 2.5,
                             "leave_days": 1, "late_count": 2, "total_late_minutes": 65,
                             "total_overtime_minutes": 40, "attendance_pct": 10.87})
_, r = call("GET", "/analytics/employees/EMP9004/monthly?month=2026-07")
check("mid-month joiner: working days from joined_on", (r["working_days"], r["present_days"], r["attendance_pct"]), (15, 1.5, 10.0))
_, r = call("GET", "/analytics/employees/EMP9005/monthly?month=2026-07")
check("employee with no logs", (r["working_days"], r["present_days"], r["late_count"]), (13, 0, 0))
_, r = call("GET", "/analytics/employees/EMP9005/monthly?month=2026-06")
check("joined after the month: no working days, null pct", (r["working_days"], r["attendance_pct"]), (0, None))
check("unknown employee -> 404", call("GET", "/analytics/employees/EMP0000/monthly?month=2026-07")[0], 404)

print("\n== department summary")
_, r = call("GET", "/analytics/departments/summary?month=2026-07&department=ZZ-Smoke-A")
# present: 9001 = 2.5, 9002 = 1 (Mon) + 1 (ON_DUTY) = 2, 9003 = 1  -> 5.5
# work-hour records (present status, has hours): 9.0, 8.0, 3.5, 5.0, 8.5, 7.25, 8.0 -> 49.25 / 7 = 7.0357 -> 7.04
check("dept A", r["items"], [{"department": "ZZ-Smoke-A", "headcount": 3, "present_days": 5.5, "avg_work_hours": 7.04,
                              "late_count": 4, "total_late_minutes": 155, "leave_count": 1, "on_duty_count": 1}])
_, r = call("GET", "/analytics/departments/summary?month=2026-07&department=ZZ-Smoke-B")
# 9005 has no logs but joined 07-15 (<= 07-31) so headcount is 2
check("dept B headcount counts the no-log employee", (r["items"][0]["headcount"], r["items"][0]["present_days"]), (2, 1.5))
_, r = call("GET", "/analytics/departments/summary?month=2026-06&department=ZZ-Smoke-B")
check("dept with nobody joined yet is omitted", r["items"], [])

print("\n== late leaderboard")
_, r = call("GET", "/analytics/leaderboard/late?month=2026-07&department=ZZ-Smoke-A&limit=2")
check("tie at the cutoff keeps both rows", [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in r["items"]],
      [(1, "EMP9001", 65), (2, "EMP9002", 45), (2, "EMP9003", 45)])
_, r = call("GET", "/analytics/leaderboard/late?month=2026-07&department=ZZ-Smoke-A&limit=1")
check("limit 1", [i["emp_code"] for i in r["items"]], ["EMP9001"])

print("\n== department trend")
_, r = call("GET", "/analytics/departments/ZZ-Smoke-B/trend?from=2026-07-10&to=2026-07-16")
rows = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"],
         i["attendance_rate"], i["moving_avg_7d"]) for i in r["items"]]
check("dept B trend (one row per day, gaps filled)", rows, [
    ("2026-07-10", True, 0, 0.0, 0, None, None),
    ("2026-07-11", False, 0, 0.0, 0, None, None),
    ("2026-07-12", False, 0, 0.0, 0, None, None),
    ("2026-07-13", True, 1, 0.5, 0, 0.5, 0.5),
    ("2026-07-14", True, 1, 1.0, 1, 1.0, 0.75),
    ("2026-07-15", True, 2, 0.0, 0, 0.0, 0.5),
    ("2026-07-16", True, 2, 0.0, 0, 0.0, 0.375),
])
_, r = call("GET", "/analytics/departments/ZZ-Smoke-A/trend?from=2026-07-06&to=2026-07-12")
check("dept A rates", [i["attendance_rate"] for i in r["items"]], [1.0, 0.6667, 0.1667, 0.0, 0.0, None, None])
check("dept A weekend row counts presence and lateness",
      (r["items"][5]["present_count"], r["items"][5]["late_count"]), (1.0, 1))
check("dept A moving avg (mean of the reported rates, half-up)", [i["moving_avg_7d"] for i in r["items"]],
      [1.0, 0.8334, 0.6111, 0.4584, 0.3667, 0.3667, 0.3667])
check("trend unknown department -> 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02")[0], 404)
check("trend 92 days ok", call("GET", "/analytics/departments/ZZ-Smoke-A/trend?from=2026-07-01&to=2026-09-30")[0], 200)

print("\n== explain uses indexes")
cases = {
    "attendance_list": "",
    "employee_monthly": "emp_code=EMP9001&month=2026-07",
    "department_summary": "month=2026-07",
    "late_leaderboard": "month=2026-07",
    "department_trend": "department=ZZ-Smoke-A&from=2026-07-01&to=2026-07-31",
}
for name, qs in cases.items():
    status, r = call("GET", f"/admin/explain/{name}?{qs}")
    text = json.dumps(r)
    check(f"explain {name}: IXSCAN and no COLLSCAN", (status, "IXSCAN" in text, "COLLSCAN" in text), (200, True, False))

# --------------------------------------------------------------------------- #
# Punch-in / punch-out / regularize on an overnight shift, and the races
# --------------------------------------------------------------------------- #
print("\n== overnight shift lifecycle")
body = {"emp_code": "EMP9101", "name": "Night Owl", "email": "night@example.com", "department": "ZZ-Smoke-C",
        "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-03-10"}
check("create employee", call("POST", "/employees", body)[0], 201)
check("duplicate emp_code -> 409", call("POST", "/employees", body)[0], 409)

s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 6, 21, 55)})
check("punch-in 21:55 IST", (s, r["date"], r["late_minutes"], r["work_hours"]), (201, "2026-07-06", 0, None))
check("second punch-in same date -> 409", call("POST", "/attendance/punch-in", {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 6, 22, 5)})[0], 409)
check("unknown employee -> 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP0000"})[0], 404)
check("punch-out earlier than any punch-in -> 404", call("POST", "/attendance/punch-out", {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 6, 21, 0)})[0], 404)

s, r = call("POST", "/attendance/punch-out", {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 7, 6, 40)})
check("punch-out next morning", (s, r["date"], r["work_hours"], r["overtime_minutes"], r["half_day"]), (200, "2026-07-06", 8.75, 40, False))
check("second punch-out -> 409", call("POST", "/attendance/punch-out", {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 7, 7, 0)})[0], 409)

s, r = call("PATCH", "/attendance/EMP9101/2026-07-06",
            {"punch_in": ms(2026, 7, 6, 22, 30), "reason": "late bus, approved", "regularized_by": "hr.admin"})
check("regularize punch_in", (s, r["late_minutes"], r["work_hours"], len(r["history"])), (200, 30, 8.17, 1))
check("history lists only changed fields", sorted(r["history"][0]["changes"]), ["late_minutes", "punch_in", "work_hours"])
check("history from/to are epoch ms", r["history"][0]["changes"]["punch_in"], {"from": ms(2026, 7, 6, 21, 55), "to": ms(2026, 7, 6, 22, 30)})
check("no-op patch -> 422", call("PATCH", "/attendance/EMP9101/2026-07-06",
      {"punch_in": ms(2026, 7, 6, 22, 30), "reason": "same again", "regularized_by": "hr"})[0], 422)
check("punch_in moved to another date -> 422", call("PATCH", "/attendance/EMP9101/2026-07-06",
      {"punch_in": ms(2026, 7, 8, 22, 30), "reason": "wrong day", "regularized_by": "hr"})[0], 422)
check("ABSENT with punch times -> 422", call("PATCH", "/attendance/EMP9101/2026-07-06",
      {"status": "ABSENT", "punch_in": ms(2026, 7, 6, 22, 0), "reason": "contradiction", "regularized_by": "hr"})[0], 422)
check("missing record -> 404", call("PATCH", "/attendance/EMP9101/2026-07-20",
      {"status": "LEAVE", "reason": "no such day", "regularized_by": "hr"})[0], 404)
s, r = call("PATCH", "/attendance/EMP9101/2026-07-06", {"status": "LEAVE", "reason": "turned out on leave", "regularized_by": "hr"})
check("status -> LEAVE clears times", (s, r["punch_in"], r["punch_out"], r["work_hours"], len(r["history"])), (200, None, None, None, 2))

print("\n== races")
db.attendance_logs.delete_many({"emp_code": "EMP9101"})
with ThreadPoolExecutor(12) as pool:
    codes = list(pool.map(lambda _: call("POST", "/attendance/punch-in",
                 {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 13, 22, 0)})[0], range(12)))
check("12 simultaneous punch-ins -> one 201, rest 409", (codes.count(201), codes.count(409)), (1, 11))
with ThreadPoolExecutor(12) as pool:
    codes = list(pool.map(lambda _: call("POST", "/attendance/punch-out",
                 {"emp_code": "EMP9101", "punched_at": ms(2026, 7, 14, 6, 0)})[0], range(12)))
check("12 simultaneous punch-outs -> one 200, rest 409", (codes.count(200), codes.count(409)), (1, 11))
with ThreadPoolExecutor(8) as pool:
    results = list(pool.map(lambda i: call("PATCH", "/attendance/EMP9101/2026-07-13",
        {"punch_out": ms(2026, 7, 14, 5, 0) + i * 60_000, "reason": f"edit number {i}", "regularized_by": "hr"})[0], range(8)))
_, rec = call("GET", "/attendance?emp_code=EMP9101&date_from=2026-07-13&date_to=2026-07-13")
check("concurrent patches never lose a history entry",
      (set(results) <= {200, 409}, len(rec["items"][0]["history"]) == results.count(200)), (True, True))
with ThreadPoolExecutor(12) as pool:
    codes = list(pool.map(lambda _: call("POST", "/employees", {**body, "emp_code": "EMP9102"})[0], range(12)))
check("12 simultaneous employee creates -> one 201, rest 409", (codes.count(201), codes.count(409)), (1, 11))
db.employees.delete_many({"emp_code": "EMP9102"})

print("\n== attendance list")
_, r = call("GET", "/attendance?date_from=2026-07-06&date_to=2026-07-06&page_size=100")
check("sorted by date desc then emp_code asc", [i["emp_code"] for i in r["items"]] == sorted(i["emp_code"] for i in r["items"]), True)
_, r = call("GET", "/attendance?emp_code=EMP9001&status=LEAVE")
check("filters + total", (r["total"], len(r["items"]), r["items"][0]["date"]), (1, 1, "2026-07-09"))

print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
