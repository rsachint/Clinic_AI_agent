import json
from datetime import datetime, timezone


class ProposalNotFound(Exception):
    pass


class ProposalAlreadyResolved(Exception):
    pass


def propose(conn, intent, slots, source_text=None):
    cur = conn.execute(
        "INSERT INTO proposals (intent, slots_json, source_text) VALUES (?, ?, ?)",
        (intent, json.dumps(slots, ensure_ascii=False), source_text),
    )
    conn.commit()
    return cur.lastrowid


def _get_pending_proposal(conn, proposal_id):
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise ProposalNotFound(proposal_id)
    if row["status"] != "pending":
        raise ProposalAlreadyResolved("proposal {} is already {}".format(proposal_id, row["status"]))
    return row


def confirm(conn, proposal_id, handlers):
    # BEGIN IMMEDIATE takes SQLite's write lock up front, so a handler's
    # "is this slot free?" check and the INSERT that follows are one atomic
    # step across every connection/thread (two patients asking for the same
    # slot at the same instant cannot both pass the check). If the caller is
    # already inside a transaction we join it, as before.
    began = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    try:
        row = _get_pending_proposal(conn, proposal_id)
        handler = handlers[row["intent"]]
        slots = json.loads(row["slots_json"])
    except BaseException:
        if began:
            conn.rollback()
        raise
    now = datetime.now(timezone.utc).isoformat()

    with conn:
        entity_type, entity_id, payload = handler(conn, slots)
        conn.execute(
            "UPDATE proposals SET status = 'confirmed', resolved_at = ?, "
            "result_entity_type = ?, result_entity_id = ? WHERE id = ?",
            (now, entity_type, entity_id, proposal_id),
        )
        conn.execute(
            "INSERT INTO audit_log (proposal_id, intent, entity_type, entity_id, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (proposal_id, row["intent"], entity_type, entity_id, json.dumps(payload, ensure_ascii=False)),
        )
    return entity_type, entity_id


def reject(conn, proposal_id):
    _get_pending_proposal(conn, proposal_id)
    conn.execute(
        "UPDATE proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), proposal_id),
    )
    conn.commit()
