import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns added to `appointments` after the first release. schema.sql's
# CREATE TABLE already has them (fresh databases), but CREATE TABLE IF NOT
# EXISTS never touches an existing table, so a database created before these
# columns existed needs them added in place. Strictly additive: no table
# rebuild, no change to any existing column or CHECK constraint.
_APPOINTMENT_COLUMNS = (
    ("queue_state", "TEXT"),
    ("last_notified_token", "INTEGER"),
    ("branch_id", "INTEGER"),
    ("doctor_id", "INTEGER"),
)

# Same idea for the WhatsApp conversation agent: table -> [(column, decl)].
# A table that does not exist in the connected database is skipped (so an
# old, partial schema is never an error).
_ADDED_COLUMNS = {
    "appointments": _APPOINTMENT_COLUMNS,
    "wa_messages": (("agent_handled", "INTEGER DEFAULT 0"),),
    "notifications": (("interactive_json", "TEXT"),),
    "booking_blocks": (("branch_id", "INTEGER"), ("doctor_id", "INTEGER")),
    "staff": (("branch_id", "INTEGER"),),
    "slot_holds": (("branch_id", "INTEGER"),),
}


def ensure_columns(conn):
    """Idempotently add any missing post-release columns. Safe to call on
    every connect(), on a fresh DB, and from concurrent connections (a
    racing 'duplicate column name' is treated as success)."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute("PRAGMA table_info({})".format(table))}
        if not existing:
            continue  # table absent
        for name, decl in columns:
            if name in existing:
                continue
            try:
                conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, name, decl))
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    conn.commit()


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    ensure_columns(conn)
    return conn
