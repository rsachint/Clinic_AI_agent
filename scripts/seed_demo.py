"""Create a demo database full of FAKE data, so the app has something to show.

    PYTHONPATH=. .venv/bin/python scripts/seed_demo.py            # writes clinic.db
    PYTHONPATH=. .venv/bin/python scripts/seed_demo.py --db demo.db

Everything below is invented: names, phone numbers (the 98765 00xxx range),
fees and appointments. Dates are relative to today, so the Queue, Appointments
and "today's appointments" views always have content.

It refuses to touch a database that already holds patients, so it can never
overwrite real data. (Delete the file yourself first if you really want a
fresh demo.)
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import db  # noqa: E402

PATIENTS = [
    ("Sunita Devi", "9876500201", 54),
    ("Rakesh Verma", "9876500202", 47),
    ("Priya Shah", "9876500203", 29),
    ("Mohan Lal", "9876500204", 61),
    ("Mohan Das", "9876500205", 38),
    ("Neeta Jha", "9876500206", 33),
    ("Amit Anand", "9876500207", 41),
    ("राहुल शर्मा", "9876500208", 36),
    ("प्रिया सिंह", "9876500209", 25),
    ("Imran Khan", "9876500210", 44),
]
STAFF = [("Seema Rao", "nurse", "9876500301"), ("Rahul Gupta", "compounder", "9876500302"), ("Kavita Nair", "receptionist", "9876500303")]


def seed(conn):
    today = date.today()

    def day(offset):
        return (today + timedelta(days=offset)).isoformat()

    pid = {}
    for name, phone, age in PATIENTS:
        cur = conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, ?)", (name, phone, age))
        pid[name] = cur.lastrowid

    for name, role, phone in STAFF:
        cur = conn.execute("INSERT INTO staff (name, role, phone) VALUES (?, ?, ?)", (name, role, phone))
        conn.execute("INSERT INTO attendance (staff_id, attendance_date, status) VALUES (?, ?, 'present')", (cur.lastrowid, day(0)))

    # (patient name, walk-in name, offset in days, start time, status)
    appointments = [
        ("Sunita Devi", None, 0, "09:00", "completed"),
        ("Rakesh Verma", None, 0, "09:30", "completed"),
        ("Priya Shah", None, 0, "10:30", "booked"),
        (None, "Walk-in: Ajay Kumar", 0, "11:00", "booked"),
        ("Mohan Lal", None, 0, "16:00", "booked"),
        ("Neeta Jha", None, 0, "17:30", "booked"),
        ("Amit Anand", None, 1, "10:00", "booked"),
        ("राहुल शर्मा", None, 1, "11:30", "booked"),
        ("Mohan Das", None, 2, "16:30", "booked"),
        (None, "Soni", 2, "17:00", "booked"),
        ("प्रिया सिंह", None, 3, "09:30", "booked"),
        ("Imran Khan", None, 4, "18:00", "booked"),
        ("Sunita Devi", None, -3, "10:00", "completed"),
        ("Rakesh Verma", None, -2, "11:00", "no_show"),
    ]
    for patient, walk_in, offset, start, status in appointments:
        if patient:
            conn.execute(
                "INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status) VALUES (?, ?, ?, 30, ?)",
                (pid[patient], day(offset), start, status))
        else:
            conn.execute(
                "INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, duration_minutes, status) "
                "VALUES (?, ?, ?, ?, 30, ?)", (walk_in.replace("Walk-in: ", ""), "9876500399", day(offset), start, status))

    # today's money (the cashbook) and one overdue follow-up (the "Missed follow-ups" card)
    for name, fee in (("Sunita Devi", 400), ("Rakesh Verma", 500)):
        conn.execute("INSERT INTO visits (patient_id, visit_date, fee_paise, notes) VALUES (?, ?, ?, 'demo')", (pid[name], day(0), fee * 100))
    conn.execute("INSERT INTO expenses (expense_date, description, amount_paise, paid_to) VALUES (?, 'Electricity bill', 120000, 'Power company')", (day(0),))
    conn.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (?, ?, 'pending')", (pid["Neeta Jha"], day(-2)))
    conn.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (?, ?, 'pending')", (pid["Imran Khan"], day(5)))
    conn.commit()
    return len(PATIENTS), len(appointments)


def main():
    parser = argparse.ArgumentParser(description="Create a demo database with fake data.")
    parser.add_argument("--db", default=os.environ.get("CLINIC_DB_PATH", "clinic.db"))
    args = parser.parse_args()

    if Path(args.db).exists():
        existing = sqlite3.connect(args.db)
        try:
            has_data = existing.execute("SELECT COUNT(*) FROM patients").fetchone()[0] > 0
        except sqlite3.OperationalError:
            has_data = False
        existing.close()
        if has_data:
            sys.exit("{} already contains patients; refusing to touch it. Use --db <new file>, "
                     "or delete the file first if you want a fresh demo.".format(args.db))

    conn = db.connect(args.db)
    patients, appointments = seed(conn)
    conn.close()
    print("Created {} with {} fake patients and {} appointments.".format(args.db, patients, appointments))


if __name__ == "__main__":
    main()
