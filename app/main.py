"""Employee Attendance & Analytics API.

Run from the repository root with: uvicorn app.main:app --port 8000
Configure MONGO_URI and MONGO_DB through environment variables or a local .env.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def to_api_value(value):
    """Convert MongoDB datetimes to the contract's epoch-millisecond format."""
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=IST) if value.tzinfo is None else value
        return int(aware.timestamp() * 1000)
    if isinstance(value, dict):
        return {key: to_api_value(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [to_api_value(item) for item in value]
    return value

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # Employee lookup and uniqueness
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )

    # Attendance lookup by employee and date
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date",
    )

    # Attendance filtering by date
    db.attendance_logs.create_index(
        [("date", 1)],
        name="ix_attendance_date",
    )



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    punch_minutes = punch_in.hour * 60 + punch_in.minute

    start = punch_in.replace(
        hour=start_h,
        minute=start_m,
        second=0,
        microsecond=0,
    )

    # For an overnight shift, an early-morning punch belongs
    # to the shift that started on the previous day.
    if end_minutes <= start_minutes and punch_minutes < end_minutes:
        start -= timedelta(days=1)

    elapsed_seconds = (punch_in - start).total_seconds()

    # Arrivals more than 10 minutes late return the full minutes.
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)

    return 0




def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")
    rounded_hours = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(rounded_hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST).replace(microsecond=0).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="Employee code already exists") from exc
    doc.pop("_id", None)
    return to_api_value(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
    db.employees.find(q, {"_id": 0})
    .sort("emp_code", 1)
    .skip(skip)
    .limit(page_size)
)
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = (
    datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
    if body.punched_at is not None
    else datetime.now(IST)
)
    ts = ts.replace(tzinfo=None, microsecond=0)
    d = ts.date().isoformat()

    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    punch_minutes = ts.hour * 60 + ts.minute

    if end_minutes <= start_minutes and punch_minutes < end_minutes:
        d = (ts.date() - timedelta(days=1)).isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    doc.pop("_id", None)
    return to_api_value(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    # Count matching records in MongoDB.
    total = db.attendance_logs.count_documents(q)

    # Sort and paginate in MongoDB instead of loading every record.
    docs = list(
        db.attendance_logs.find(q)
        .sort("date", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return to_api_value({
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
    datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
    if body.punched_at is not None
    else datetime.now(IST)
)
    ts = ts.replace(tzinfo=None, microsecond=0)     

    # Find the most recent punch-in at or before punch-out time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_time = record["punch_in"]
    elapsed_seconds = (ts - punch_in_time).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_time, ts)

    h, m = map(int, emp["shift_end"].split(":"))
    shift_end = datetime.fromisoformat(record["date"]).replace(
        hour=h, minute=m
    )

    # For overnight shifts, the shift end may fall on the next day.
    if shift_end < punch_in_time:
        shift_end += timedelta(days=1)

    overtime_minutes = max(
    0, int((ts - shift_end).total_seconds() // 60)
)
    if overtime_minutes < 30:
        overtime_minutes = 0
    half_day = work_hours < 4.50

    # Atomic update: only one concurrent request can close this record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return to_api_value(updated)



@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    from datetime import date as date_type

    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Keep the existing values unless the request supplies a replacement.
    old_status = record.get("status")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    new_status = body.status if body.status is not None else old_status
    new_in = old_in
    new_out = old_out

    if body.punch_in is not None:
        new_in = datetime.fromtimestamp(body.punch_in / 1000, tz=IST)
        new_in = new_in.replace(tzinfo=None, microsecond=0)

    if body.punch_out is not None:
        new_out = datetime.fromtimestamp(body.punch_out / 1000, tz=IST)
        new_out = new_out.replace(tzinfo=None, microsecond=0)

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if new_status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid status")

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot include punch times",
            )
        new_in = None
        new_out = None
    elif new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in",
        )

    if new_in is not None:
        # The punch-in must belong to the attendance date in IST.
        if new_in.date().isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must be on the attendance date",
            )

    if new_out is not None:
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in",
            )
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    late = 0
    work_hours = None
    overtime = 0
    half_day = False

    if new_in is not None:
        
        late = compute_late_minutes(
        new_in,
        emp["shift_start"],
        emp["shift_end"],
)


    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)

        h, m = map(int, emp["shift_end"].split(":"))
        shift_end = datetime.fromisoformat(date).replace(hour=h, minute=m)

        # If the shift ends on the next calendar day, advance it.
        if shift_end <= new_in:
            shift_end += timedelta(days=1)

        overtime = max(0, int((new_out - shift_end).total_seconds() // 60))
        if overtime < 30:
            overtime = 0

        half_day = work_hours < 4.50

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        old_value = record.get(field)
        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": datetime.now(IST).replace(microsecond=0).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Conditional update protects against overwriting a concurrent correction.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "history": record.get("history", []),
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Record changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return to_api_value(updated)



from decimal import Decimal, ROUND_HALF_UP


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum, rounding=ROUND_HALF_UP
        )
    )


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    from calendar import monthrange
    from datetime import date as date_type

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        year, month_num = map(int, month.split("-"))
        first_day = date_type(year, month_num, 1)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid month")

    last_day_num = monthrange(year, month_num)[1]
    last_day = date_type(year, month_num, last_day_num)

    # Working days are Monday-Friday, starting from joined_on.
    joined_on = date_type.fromisoformat(emp["joined_on"])
    working_days = 0
    current_day = max(first_day, joined_on)

    while current_day <= last_day:
        if current_day.weekday() < 5:
            working_days += 1
        current_day = date_type.fromordinal(current_day.toordinal() + 1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    
{
    "$and": [
        {
            "$gte": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                2,
            ]
        },
        {
            "$lte": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                6,
            ]
        },
    ]
}
,
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    stats = results[0] if results else {}

    present_days = stats.get("present_days", 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 4)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    
    from calendar import monthrange
    from datetime import date as date_type

    try:
        year, month_num = map(int, month.split("-"))
        first_day = date_type(year, month_num, 1)
        last_day = date_type(
            year, month_num, monthrange(year, month_num)[1]
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$lt": [
                                        {"$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }},
                                        7,
                                    ]},
                                    {"$gt": [
                                        {"$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }},
                                        1,
                                    ]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                
"avg_work_hours": {
    "$avg": {
        "$cond": [
            {
                "$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$work_hours", None]},
                ]
            },
            "$work_hours",
            None,
        ]
    }
},

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    stats = list(db.attendance_logs.aggregate(pipeline))

    # Headcount is based on employees who joined by the end of the month.
    departments = list(
        db.employees.aggregate([
            {"$match": {"joined_on": {"$lte": last_day.isoformat()}}},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                }
            },
        ])
    )
    headcounts = {item["_id"]: item["headcount"] for item in departments}

    
    stats_by_department = {row["_id"]: row for row in stats}

    
    

    items = []
    for department, headcount in sorted(headcounts.items()):
        row = stats_by_department.get(department, {})

        items.append({
            "department": department,
            "headcount": headcount,
            "present_days": row.get("present_days", 0),
            "avg_work_hours": (
                round_half_up(row["avg_work_hours"], 2)
                if row.get("avg_work_hours") is not None
                else None
            ),
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}




@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    from calendar import monthrange
    from datetime import date as date_type

    try:
        year, month_num = map(int, month.split("-"))
        first_day = date_type(year, month_num, 1)
        last_day = date_type(
            year, month_num, monthrange(year, month_num)[1]
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    from datetime import date as date_type, timedelta

    try:
        start_date = date_type.fromisoformat(from_date)
        end_date = date_type.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date")

    days = (end_date - start_date).days + 1
    if days <= 0 or days > 92:
        raise HTTPException(
            status_code=422,
            detail="Invalid date range: to must be >= from and range <= 92 days",
        )

    employee_count = db.employees.count_documents(
        {"department": department}
    )
    if employee_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": from_date,
                    "$lte": to_date,
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"date": 1}},

{
    "$set": {
        "date": {
            "$dateFromString": {
                "dateString": "$date",
                "format": "%Y-%m-%d",
            }
        }
    }
},
{
    "$densify": {
        "field": "date",
        "range": {
            "step": 1,
            "unit": "day",
            "bounds": [
                datetime.combine(start_date, datetime.min.time()),
                datetime.combine(end_date + timedelta(days=1), datetime.min.time()),          ],
        },
    }
},
{
    "$set": {
        "date": {
            "$dateToString": {
                "format": "%Y-%m-%d",
                "date": "$date",
                "timezone": "UTC",
            }
        }
    }
},

        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                },
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                },
                                6,
                            ]
                        },
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$literal": list(
                                    db.employees.find(
                                        {
                                            "department": department,
                                            "joined_on": {
                                                "$lte": to_date
                                            },
                                        },
                                        {"joined_on": 1, "_id": 0},
                                    )
                                )
                            },
                            "as": "employee",
                            "cond": {
    "$lte": [
        "$$employee.joined_on",
        "$date",
    ]
}
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(
                item["attendance_rate"], 4
            )
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(
                item["moving_avg_7d"], 4
            )

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if from_date or to_date:
            query["date"] = {}
            if from_date:
                query["date"]["$gte"] = from_date
            if to_date:
                query["date"]["$lte"] = to_date

        explanation = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1},
            },
            verbosity="executionStats",
        )

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        try:
            year, mon = map(int, month.split("-"))
            if mon < 1 or mon > 12:
                raise ValueError
            start = f"{year:04d}-{mon:02d}-01"
            next_month = datetime(
                year + (mon == 12),
                1 if mon == 12 else mon + 1,
                1,
            )
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Invalid month; use YYYY-MM",
            )

        end = next_month.strftime("%Y-%m-%d")

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start, "$lt": end},
                }
            },
            {"$sort": {"date": 1}},
        ]

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint in {"department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        try:
            year, mon = map(int, month.split("-"))
            if mon < 1 or mon > 12:
                raise ValueError
            start = f"{year:04d}-{mon:02d}-01"
            next_month = datetime(
                year + (mon == 12),
                1 if mon == 12 else mon + 1,
                1,
            )
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Invalid month; use YYYY-MM",
            )

        end = next_month.strftime("%Y-%m-%d")

        if endpoint == "department_summary":
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": start, "$lt": end}
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
                {
                    "$group": {
                        "_id": "$employee.department",
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        },
                    }
                },
            ]
        else:
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": start, "$lt": end},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
            ]

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    import json
    from bson import json_util

    return json.loads(json_util.dumps(explanation))



    import json
    from bson import json_util

    return json.loads(json_util.dumps(explanation))



