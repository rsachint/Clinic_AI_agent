"""The fake clinic the replay conversations run in. Every name and number is made up.

Clock: Friday 2026-10-09, 10:00 IST (scripts/replay/clock.py). Branches A (Dr. Mehta 09-13 and 16-20), B (Dr. Rao
10-14), C (Dr. Iyer 09-12), every day. A conversation's `setup` can add to this (or take patients away with
`without`); patients and appointments are referred to by KEY in the expectations, never by a database id.
"""

TODAY = "2026-10-09"
TOMORROW = "2026-10-10"      # Saturday
SUNDAY = "2026-10-11"
MONDAY = "2026-10-12"
TUESDAY = "2026-10-13"
WEDNESDAY = "2026-10-14"
FRIDAY_NEXT = "2026-10-16"

BRANCHES = [
    {"code": "B", "name": "Branch B", "address": "Sector 56, Gurugram", "pin_code": "122011", "doctor": "Dr. Rao",
     "hours": ("10:00", "14:00")},
    {"code": "C", "name": "Branch C", "address": "MG Road", "pin_code": "122018", "doctor": "Dr. Iyer",
     "hours": ("09:00", "12:00")},
]

PATIENTS = [
    {"key": "rahul_dev", "name": "राहुल शर्मा", "phone": "9876543210", "age": 41},
    {"key": "rahul_en", "name": "Rahul Sharma", "phone": "9876546543", "age": 29},
    {"key": "priya_shah", "name": "Priya Shah", "phone": "9123499999", "age": 34},
    {"key": "priya_dev", "name": "प्रिया शर्मा", "phone": "9123456780", "age": 27},
    {"key": "amit_dua", "name": "Amit Dua", "phone": "9811122233", "age": 45},
    {"key": "amit_anand", "name": "Amit Anand", "phone": "9811144455", "age": 38},
    {"key": "manju", "name": "Manju", "phone": "9000000018", "age": 52},
    {"key": "nalin", "name": "Nalin", "phone": "9000000077", "age": 31},
    {"key": "sunita", "name": "Sunita Devi", "phone": "9000000004", "age": 60},
    {"key": "rakesh", "name": "Rakesh Verma", "phone": "9000000001", "age": 40},
    {"key": "mohan_lal", "name": "Mohan Lal", "phone": "9000000002", "age": 55},
    {"key": "mohan_das", "name": "Mohan Das", "phone": "9000000003", "age": 47},
]

STAFF = [{"key": "seema", "name": "Seema", "role": "nurse"}]

# Saturday's list at Branch A (what "who is booked tomorrow" shows: Sunita 10:00, Rakesh 11:00, Manju 12:00),
# Manju also has one on Monday; Amit Dua and Amit Anand each have a Monday appointment; Priya Shah has none.
APPOINTMENTS = [
    {"key": "sunita_sat", "patient": "sunita", "date": TOMORROW, "time": "10:00", "branch": "A"},
    {"key": "rakesh_sat", "patient": "rakesh", "date": TOMORROW, "time": "11:00", "branch": "A"},
    {"key": "manju_sat", "patient": "manju", "date": TOMORROW, "time": "12:00", "branch": "A"},
    {"key": "amit_dua_mon", "patient": "amit_dua", "date": MONDAY, "time": "11:00", "branch": "A"},
    {"key": "amit_anand_mon", "patient": "amit_anand", "date": MONDAY, "time": "16:00", "branch": "A"},
]

BASE_SETUP = {"branches": BRANCHES, "patients": PATIENTS, "staff": STAFF, "appointments": APPOINTMENTS}


def with_setup(**changes):
    """The base clinic with some changes. `without=["nalin"]` removes patients (and their appointments);
    `without_appointments=True` removes every appointment; any other key replaces that part of the base."""
    setup = {k: list(v) for k, v in BASE_SETUP.items()}
    without = set(changes.pop("without", ()))
    if without:
        setup["patients"] = [p for p in setup["patients"] if p["key"] not in without]
        setup["appointments"] = [a for a in setup["appointments"] if a["patient"] not in without]
    if changes.pop("without_appointments", False):
        setup["appointments"] = []
    setup.update(changes)
    return setup
