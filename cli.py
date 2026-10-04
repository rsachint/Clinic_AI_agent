#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from clinic import core
from clinic.adapters.registry import build_write_handlers, get_adapters
from clinic.asr import transcribe
from clinic.mic import play_beep, record
from clinic.db import connect
from clinic.pipeline import NavigateResult, PipelineError, ReadResult, transcript_to_response
from clinic.tts import synthesize

DB_PATH = "clinic.db"

CLINICAL_ADAPTER, OPS_ADAPTER = get_adapters()
HANDLERS = build_write_handlers(CLINICAL_ADAPTER, OPS_ADAPTER)


def _confirm_prompt(conn, proposal_id, description):
    print("Proposal #{}: {}".format(proposal_id, description))
    if input("Confirm? [y/N] ").strip().lower() != "y":
        core.reject(conn, proposal_id)
        print("Rejected.")
        return
    entity_type, entity_id = core.confirm(conn, proposal_id, HANDLERS)
    print("Committed {} #{}".format(entity_type, entity_id))


def _pick_patient(conn, query):
    candidates = CLINICAL_ADAPTER.resolve_patient(conn, query)
    if not candidates or candidates[0].score < 0.6:
        print("No confident match for '{}'. Closest guesses:".format(query))
        for c in candidates:
            print("  #{} {} (score {:.2f})".format(c.id, c.label, c.score))
        sys.exit(1)
    if len(candidates) > 1 and candidates[0].score - candidates[1].score < 0.1:
        print("Ambiguous match for '{}':".format(query))
        for c in candidates:
            print("  #{} {} (score {:.2f})".format(c.id, c.label, c.score))
        sys.exit(1)
    return candidates[0]


def _pick_staff(conn, query):
    candidates = OPS_ADAPTER.resolve_staff(conn, query)
    if not candidates or candidates[0].score < 0.6:
        print("No confident staff match for '{}'. Closest guesses:".format(query))
        for c in candidates:
            print("  #{} {} (score {:.2f})".format(c.id, c.label, c.score))
        sys.exit(1)
    return candidates[0]


def cmd_register_patient(conn, args):
    pid = core.propose(
        conn, "register_patient",
        {"name": args.name, "phone": args.phone, "age": args.age},
        source_text=" ".join(sys.argv),
    )
    _confirm_prompt(conn, pid, "register {} ({}), age {}".format(args.name, args.phone, args.age))


def cmd_register_staff(conn, args):
    pid = core.propose(
        conn, "register_staff",
        {"name": args.name, "role": args.role, "phone": args.phone},
        source_text=" ".join(sys.argv),
    )
    _confirm_prompt(conn, pid, "register staff {} ({})".format(args.name, args.role))


def cmd_visit(conn, args):
    patient = _pick_patient(conn, args.patient)
    pid = core.propose(
        conn, "record_visit",
        {"patient_id": patient.id, "fee_rupees": args.fee, "visit_date": args.date, "notes": args.notes},
    )
    _confirm_prompt(conn, pid, "visit for {} — Rs {}".format(patient.label, args.fee))


def cmd_followup(conn, args):
    patient = _pick_patient(conn, args.patient)
    pid = core.propose(
        conn, "set_followup",
        {"patient_id": patient.id, "days_from_now": args.days},
    )
    _confirm_prompt(conn, pid, "follow-up for {} in {} days".format(patient.label, args.days))

def cmd_missed(conn, args):
    for row in CLINICAL_ADAPTER.missed_followups(conn, as_of=args.as_of):
        print("{}  {} ({}) — due {}".format(row["id"], row["name"], row["phone"], row["due_date"]))


def cmd_cashbook(conn, args):
    totals = OPS_ADAPTER.day_end_cashbook(conn, on_date=args.date)
    print("{date}: fees Rs {fees:.2f}  expenses Rs {expenses:.2f}  net Rs {net:.2f}".format(
        date=totals["date"],
        fees=totals["fees_paise"] / 100,
        expenses=totals["expenses_paise"] / 100,
        net=totals["net_paise"] / 100,
    ))



def cmd_listen(conn, args):
    path = "recording.wav"
    print("Recording for {} seconds -- speak now!".format(args.seconds))
    play_beep()
    record(path, args.seconds)
    print("Transcribing with Sarvam Saaras ({})...".format(args.language))
    heard = transcribe(path, language_code=args.language)
    print("Transcript: {}".format(heard.text))
    print("Detected language: {} (probability {})".format(heard.language_code, heard.language_probability))


def cmd_speak(conn, args):
    path = "recording.wav"
    print("Recording for {} seconds -- speak now!".format(args.seconds))
    play_beep()
    record(path, args.seconds)
    print("Transcribing with Sarvam Saaras ({})...".format(args.language))
    heard = transcribe(path, language_code=args.language)
    print("Heard: {} (language: {})".format(heard.text, heard.language_code))

    lang = heard.language_code or "hi-IN"
    try:
        result = transcript_to_response(conn, heard.text, CLINICAL_ADAPTER, OPS_ADAPTER, lang)
    except PipelineError as e:
        print(str(e))
        return

    if isinstance(result, NavigateResult):
        print(result.answer_text)  # the CLI has no dashboard to navigate
        return

    if isinstance(result, ReadResult):
        print(result.answer_text)
        audio = synthesize(result.answer_text, language_code=lang)
        Path("reply.wav").write_bytes(audio)
        subprocess.run(["afplay", "reply.wav"])
        return

    if result.intent == "log_attendance":
        # Only low-stakes write that skips the tap-to-confirm step (§12.3):
        # no money, medicine, or contact-info risk, so voice confirmation
        # alone is acceptable here even though it isn't for other writes.
        entity_type, entity_id = core.confirm(conn, result.proposal_id, HANDLERS)
        message = "Marked: {}".format(result.description)
        print(message)
        audio = synthesize(message, language_code=lang)
        Path("reply.wav").write_bytes(audio)
        subprocess.run(["afplay", "reply.wav"])
        return

    _confirm_prompt(conn, result.proposal_id, result.description)

def build_parser():
    parser = argparse.ArgumentParser(description="Clinic Copilot — typed command path (no voice)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register-patient")
    p.add_argument("name")
    p.add_argument("phone")
    p.add_argument("--age", type=int)
    p.set_defaults(func=cmd_register_patient)

    p = sub.add_parser("register-staff")
    p.add_argument("name")
    p.add_argument("--role")
    p.add_argument("--phone")
    p.set_defaults(func=cmd_register_staff)

    p = sub.add_parser("visit")
    p.add_argument("patient", help="name or phone to resolve against existing patients")
    p.add_argument("fee", type=float)
    p.add_argument("--date")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_visit)

    p = sub.add_parser("followup")
    p.add_argument("patient")
    p.add_argument("days", type=int)
    p.set_defaults(func=cmd_followup)

    p = sub.add_parser("missed")
    p.add_argument("--as-of", dest="as_of")
    p.set_defaults(func=cmd_missed)

    p = sub.add_parser("cashbook")
    p.add_argument("--date")
    p.set_defaults(func=cmd_cashbook)

    p = sub.add_parser("listen", help="record from the mic and transcribe via Sarvam Saaras")
    p.add_argument("--seconds", type=int, default=6)
    p.add_argument("--language", default="unknown")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("speak", help="record, transcribe, classify intent, and propose a command")
    p.add_argument("--seconds", type=int, default=8)
    p.add_argument("--language", default="unknown")
    p.set_defaults(func=cmd_speak)

    return parser


def main():
    load_dotenv()
    args = build_parser().parse_args()
    conn = connect(DB_PATH)
    args.func(conn, args)


if __name__ == "__main__":
    main()
