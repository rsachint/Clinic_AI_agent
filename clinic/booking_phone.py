"""A phone number is mandatory for every NEW appointment booking.

One place for the rule and its wording, so the write handler (clinic/intents.py),
the staff form, the follow-up planner, the automatic WhatsApp path and the review
cards all say the same thing. The handler is the authority; every other caller uses
this module to flag the problem early (a review card, a voice question, a plan row).

A booking needs an EFFECTIVE phone: the registered patient's own phone, or the
phone given on the booking. Both are read the way clinic/entity_resolution.py reads
any phone (last 10 digits, so +91 / 0 / spaces do not matter) and must then be
exactly 10 digits -- no first-digit rule: real and test numbers such as 1122334455
exist. Only NEW bookings are checked: rescheduling, cancelling, the queue and a
closure move never come through here, because an older appointment may have no phone.
"""

from clinic.entity_resolution import last10_digits

REQUIRED = "A phone number is required to book an appointment. Enter the patient's 10-digit phone number."
PATIENT_NO_PHONE = ("This patient has no valid phone number on file. A phone number is required to book an "
                    "appointment: add or correct the patient's 10-digit phone number first.")


class PhoneRequiredError(ValueError):
    """The booking was refused for want of a valid phone number. A ValueError like the
    handler's other refusals, so core.confirm, /approve and auto_actions show it as an
    ordinary failure; it is NOT a slot error, so the automatic path sends it to staff
    instead of offering the patient other times."""


def valid_phone(phone):
    """The 10 digits of `phone`, or '' when it is blank or not exactly 10 digits."""
    digits = last10_digits(str(phone)) if phone not in (None, "") else ""
    return digits if len(digits) == 10 else ""


def patient_phone_on_file(conn, patient_id):
    """(found, valid 10 digits or '') for a registered patient."""
    row = conn.execute("SELECT phone FROM patients WHERE id = ?", (patient_id,)).fetchone() if patient_id else None
    return (row is not None), valid_phone(row["phone"]) if row is not None else ""


def effective_phone(conn, slots):
    """The 10-digit phone this booking would be made under, or ''. For a registered
    patient it is their own phone, else the one given on the booking."""
    _, own = patient_phone_on_file(conn, slots.get("patient_id"))
    return own or valid_phone(slots.get("patient_phone"))


def problem(conn, slots):
    """None when the booking has a usable phone; otherwise the fixed message to show.
    A registered patient without a valid phone on file gets the variant that says so."""
    if effective_phone(conn, slots):
        return None
    found, _ = patient_phone_on_file(conn, slots.get("patient_id"))
    return PATIENT_NO_PHONE if found else REQUIRED


def check(conn, slots):
    """Raise PhoneRequiredError unless the booking has a usable phone."""
    message = problem(conn, slots)
    if message:
        raise PhoneRequiredError(message)
