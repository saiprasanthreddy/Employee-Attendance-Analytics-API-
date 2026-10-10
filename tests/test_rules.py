"""Offline tests: business rules, request validation and pipeline construction.
They need no running MongoDB.   Run:  pytest tests/test_rules.py
"""
from datetime import date, datetime, timedelta, timezone

import bson
import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import IST, UTC

client = TestClient(main.app)


def test_root_returns_service_links():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["docs"] == "/docs"


def ist(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST).astimezone(UTC)


# ---- R2 late ---------------------------------------------------------------
@pytest.mark.parametrize(
    "h, m, s, expected",
    [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0), (9, 30, 0, 0)],
)
def test_late_examples_from_contract(h, m, s, expected):
    punch = ist(2026, 7, 6, h, m, s)
    assert main.late_minutes_for(punch, "2026-07-06", "09:30") == expected


# ---- R1 attendance date ----------------------------------------------------
def test_normal_shift_date_is_ist_date_not_utc_date():
    # 00:10 IST on the 7th is still the 6th in UTC; the record belongs to the 7th.
    assert main.attendance_date(ist(2026, 7, 7, 0, 10), "09:30", "18:30") == "2026-07-07"


def test_overnight_shift_early_morning_belongs_to_previous_day():
    assert main.attendance_date(ist(2026, 7, 7, 5, 30), "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == "2026-07-07"


def test_seconds_truncation():
    assert main.ms_to_dt(1_783_312_500_999) == main.ms_to_dt(1_783_312_500_000)


# ---- R3 overtime -----------------------------------------------------------
def test_overtime_threshold_and_floor():
    day = "2026-07-06"
    assert main.overtime_minutes_for(ist(2026, 7, 6, 19, 0), day, "09:30", "18:30") == 30
    assert main.overtime_minutes_for(ist(2026, 7, 6, 18, 59, 59), day, "09:30", "18:30") == 0
    assert main.overtime_minutes_for(ist(2026, 7, 6, 19, 10, 30), day, "09:30", "18:30") == 40
    assert main.overtime_minutes_for(ist(2026, 7, 6, 17, 0), day, "09:30", "18:30") == 0


def test_overtime_overnight_shift_ends_next_day():
    out = ist(2026, 7, 7, 6, 40)
    assert main.overtime_minutes_for(out, "2026-07-06", "22:00", "06:00") == 40


# ---- R4 / R5 work hours and half day --------------------------------------
def test_work_hours_rounds_half_up_not_bankers():
    start = ist(2026, 7, 6, 9, 30)
    # 14418 s = 4.005 h exactly; half-up gives 4.01 (float round() would give 4.0)
    assert main.work_hours_for(start, start + timedelta(seconds=14418)) == 4.01
    assert main.work_hours_for(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12


def test_half_day_uses_rounded_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    start = ist(2026, 7, 6, 9, 30)
    just_under = main.derive_fields(emp, "2026-07-06", start, start + timedelta(seconds=16164))  # 4.49
    exactly = main.derive_fields(emp, "2026-07-06", start, start + timedelta(seconds=16182))  # 4.495 -> 4.50
    assert just_under["half_day"] is True
    assert exactly["work_hours"] == 4.5 and exactly["half_day"] is False


def test_open_record_has_null_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    fields = main.derive_fields(emp, "2026-07-06", ist(2026, 7, 6, 9, 30), None)
    assert fields["work_hours"] is None and fields["half_day"] is False


# ---- R7 working days -------------------------------------------------------
def test_weekday_counting():
    assert main.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert main.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner
    assert main.count_weekdays(date(2026, 8, 1), date(2026, 7, 31)) == 0


# ---- request validation (no database needed: these fail before any query) --
@pytest.mark.parametrize("bad", [1_783_312_500, 1_783_312_500_000.5, "1783312500000", True, 5])
def test_punch_in_rejects_bad_timestamps(bad):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
    assert r.status_code == 422


def test_punch_in_rejects_non_presence_status():
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert r.status_code == 422


def test_create_employee_validation():
    base = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-05"}
    assert client.post("/employees", json={**base, "emp_code": "E1"}).status_code == 422
    assert client.post("/employees", json={**base, "joined_on": "2026-02-30"}).status_code == 422
    assert client.post("/employees", json={**base, "shift_start": "25:00"}).status_code == 422
    assert client.post("/employees", json={**base, "shift_start": "09:00", "shift_end": "09:00"}).status_code == 422
    assert client.post("/employees", json={**base, "email": "nope"}).status_code == 422


def test_attendance_list_validation():
    assert client.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "07/10/2026"}).status_code == 422
    assert client.get("/attendance", params={"status": "HOLIDAY"}).status_code == 422
    assert client.get("/attendance", params={"page": 0}).status_code == 422
    assert client.get("/attendance", params={"page_size": 101}).status_code == 422


def test_analytics_validation():
    assert client.get("/analytics/departments/summary", params={"month": "2026-13"}).status_code == 422
    assert client.get("/analytics/departments/summary").status_code == 422
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422
    q = {"from": "2026-07-10", "to": "2026-07-01"}
    assert client.get("/analytics/departments/Sales/trend", params=q).status_code == 422
    q = {"from": "2026-01-01", "to": "2026-04-03"}  # 93 days
    assert client.get("/analytics/departments/Sales/trend", params=q).status_code == 422


def test_explain_requires_its_parameters():
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422
    assert client.get("/admin/explain/nonsense").status_code == 422


def test_regularize_body_validation():
    url = "/attendance/EMP0001/2026-07-07"
    ok = {"reason": "biometric glitch", "regularized_by": "hr"}
    assert client.patch(url, json={**ok, "reason": "x"}).status_code == 422
    assert client.patch(url, json={**ok, "punch_in": None}).status_code == 422
    assert client.patch(url, json={**ok, "punch_in": 1_783_312_500}).status_code == 422
    assert client.patch(url, json={**ok, "status": "HOLIDAY"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-02-30", json=ok).status_code == 422


# ---- pipelines must at least be valid BSON --------------------------------
def test_all_pipelines_encode_to_bson():
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    pipelines = [
        main.monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31"),
        main.department_summary_pipeline("2026-07", None),
        main.department_summary_pipeline("2026-07", "Sales"),
        main.late_leaderboard_pipeline("2026-07", 10, None),
        main.late_leaderboard_pipeline("2026-07", 10, "Sales"),
        main.department_trend_pipeline("Sales", start, end),
    ]
    for p in pipelines:
        bson.encode({"pipeline": p})


def test_leaderboard_filters_department_before_ranking():
    stages = main.late_leaderboard_pipeline("2026-07", 3, "Sales")
    names = [next(iter(s)) for s in stages]
    assert names.index("$match") < names.index("$setWindowFields")
    dept_match = [s for s in stages if "$match" in s and "emp.department" in s["$match"]]
    assert dept_match and stages.index(dept_match[0]) < names.index("$setWindowFields")


def test_index_specs_include_the_unique_guards():
    specs = {(c, tuple(k)): o for c, k, o in main.index_specs()}
    assert specs[("attendance_logs", (("emp_code", 1), ("date", 1)))]["unique"] is True
    assert specs[("employees", (("emp_code", 1),))]["unique"] is True
