import os

from clinic.adapters.local_sqlite import LocalSQLiteAdapter


def get_adapters():
    """Prototype-grade selection, not a plugin framework: one env var, one
    if/else. Fails loudly on anything but "local" since no second adapter
    exists yet -- silently falling back would hide a misconfiguration."""
    clinical_kind = os.environ.get("CLINICAL_ADAPTER", "local")
    if clinical_kind != "local":
        raise ValueError("Unknown CLINICAL_ADAPTER={!r}; only 'local' is implemented".format(clinical_kind))
    shared = LocalSQLiteAdapter()
    return shared, shared  # (clinical_adapter, ops_adapter) -- same instance today


def build_write_handlers(clinical_adapter, ops_adapter):
    """Same {intent: handler} shape core.confirm already expects -- just
    sourced from the registry instead of a static `from clinic.intents
    import HANDLERS`, so swapping in a real EMR adapter later only requires
    a new branch here, not changes to core.py/pipeline.py/app.py/cli.py."""
    return {
        "register_patient": clinical_adapter.register_patient,
        "record_visit": clinical_adapter.record_visit,
        "set_followup": clinical_adapter.set_followup,
        "cancel_followup": clinical_adapter.cancel_followup,
        "reschedule_followup": clinical_adapter.reschedule_followup,
        "book_appointment": clinical_adapter.book_appointment,
        "cancel_appointment": clinical_adapter.cancel_appointment,
        "reschedule_appointment": clinical_adapter.reschedule_appointment,
        "restore_appointment": clinical_adapter.restore_appointment,
        "queue_check_in": clinical_adapter.check_in,
        "queue_call_next": clinical_adapter.start_consultation,
        "queue_mark_done": clinical_adapter.complete,
        "queue_mark_no_show": clinical_adapter.mark_no_show,
        "register_staff": ops_adapter.register_staff,
        "log_attendance": ops_adapter.log_attendance,
        "log_expense": ops_adapter.log_expense,
    }
