"""Print the questions the assistant could not answer, for a developer to build.

    PYTHONPATH=. .venv/bin/python scripts/export_unanswered.py --db clinic.db

Open questions (new / building), most asked first, each with what the planner said the user
wanted and the query spec it tried and was refused (a HINT: never trusted, never run). Paste it
into a build task: add the whitelist entry to clinic/query_tool.py (and the planner prompt
line in clinic/nlu/tools.py), then mark the question resolved in the app's Audit log tab with a
one-line note ("Ask: who is on duty now"); the person who asked is told once that it works.

PRIVACY: transcripts can contain patient names. The database is opened read-only and the
output is plain text for you; keep it out of git and out of chat.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from clinic import unanswered


def open_read_only(path):
    uri = "file:{}?mode=ro".format(Path(path).resolve().as_posix())
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="clinic.db", help="the clinic database (opened read-only)")
    args = ap.parse_args(argv)
    conn = open_read_only(args.db)
    try:
        text = unanswered.export_open(conn)
    except sqlite3.OperationalError:
        print("No unanswered_questions table yet: the app creates it the first time it runs.")
        return 0
    finally:
        conn.close()
    print(text or "No open questions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
