#!/usr/bin/env python3
"""One-off migration: add 'cancelled' to followups.status's CHECK constraint.
SQLite can't ALTER a CHECK constraint, so this rebuilds the table. Back up
clinic.db before running this (see README/plan §13.1)."""
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "clinic.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")
    with conn:
        before = conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0]

        conn.execute("""
            CREATE TABLE followups_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL REFERENCES patients(id),
                visit_id INTEGER REFERENCES visits(id),
                due_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'missed', 'cancelled')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO followups_new (id, patient_id, visit_id, due_date, status, created_at, completed_at)
            SELECT id, patient_id, visit_id, due_date, status, created_at, completed_at FROM followups
        """)
        conn.execute("DROP TABLE followups")
        conn.execute("ALTER TABLE followups_new RENAME TO followups")

        after = conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0]
        assert before == after, "row count mismatch: {} before, {} after".format(before, after)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
    print("Migrated followups.status CHECK constraint on {} ({} rows preserved)".format(DB_PATH, after))


if __name__ == "__main__":
    main()
