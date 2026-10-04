"""Scripted-dialogue tests for the WhatsApp conversation agent
(clinic/conversation.py). Every scenario that has user-visible text runs in
English, Devanagari Hindi and Roman-script Hinglish. No network, no wall
clock: the clock and the LLM intent picker are injected.
"""
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import conv_templates as ct
from clinic import conversation as cv
from clinic import notify

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
NOW = datetime(2026, 10, 5, 10, 0)  # Monday 10:00
TODAY, TOMORROW, WED, FRI = "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-09"
WA = "919876543210"
WA2 = "919111122223"


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# What a patient types, per language (the same intent in each).
SAY = {
    "en": dict(hello="hello", book="I want to book an appointment", cancel="cancel my appointment",
               resched="reschedule my appointment", status="what is my token", name="Sunita Devi",
               tomorrow="tomorrow", friday="Friday", t4pm="4 pm", t9am="9 am", t11pm="11 pm",
               yes="yes", no="no", change="change", nonsense="blorp", human="I want to talk to a person",
               emergency="I have chest pain", clinical="what medicine should I take for fever",
               past="01/10/2026", book_all="book an appointment tomorrow at 4 pm",
               clinical_book="I have fever, I want to book an appointment tomorrow"),
    "hi": dict(hello="नमस्ते", book="मुझे अपॉइंटमेंट चाहिए", cancel="मेरी अपॉइंटमेंट रद्द करें",
               resched="अपॉइंटमेंट रीशेड्यूल करनी है", status="मेरा टोकन क्या है", name="सुनीता देवी",
               tomorrow="कल", friday="शुक्रवार", t4pm="शाम 4 बजे", t9am="सुबह 9 बजे", t11pm="रात 11 बजे",
               yes="हाँ", no="नहीं", change="बदलें", nonsense="ब्लॉर्प", human="मुझे किसी से बात करनी है",
               emergency="मुझे सीने में दर्द है", clinical="बुखार के लिए कौन सी दवा लूँ",
               past="०१/१०/२०२६", book_all="कल शाम 4 बजे अपॉइंटमेंट चाहिए",
               clinical_book="मुझे बुखार है, कल अपॉइंटमेंट चाहिए"),
    "hinglish": dict(hello="namaste", book="mujhe appointment chahiye", cancel="mujhe appointment cancel karni hai",
                     resched="appointment reschedule karni hai", status="mera token kya hai", name="Sunita Devi",
                     tomorrow="kal", friday="shukrawar", t4pm="shaam 4 baje", t9am="subah 9 baje",
                     t11pm="raat 11 baje", yes="haan", no="nahi", change="badlein", nonsense="blorp kya",
                     human="mujhe kisi se baat karni hai", emergency="seene mein dard ho raha hai",
                     clinical="bukhar ke liye kaun si dawai lun", past="01/10/2026",
                     book_all="kal shaam 4 baje appointment chahiye",
                     clinical_book="mujhe bukhar hai, kal appointment chahiye"),
}
WA_FOR = {"en": "919800000001", "hi": "919800000002", "hinglish": "919800000003"}


class FakePicker:
    def __init__(self, label="unclear", raises=None):
        self.label, self.raises, self.calls = label, raises, []

    def __call__(self, text):
        self.calls.append(text)
        if self.raises:
            raise self.raises
        return self.label


class ConvCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.clock = NOW
        self.picker = FakePicker()

    # -- fixtures --------------------------------------------------------------
    def patient(self, name="Sunita Devi", phone="9876543210"):
        cur = self.conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, phone))
        self.conn.commit()
        return cur.lastrowid

    def appt(self, appt_date, start, patient_id=None, name=None, phone=None, status="booked"):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, status) "
            "VALUES (?, ?, ?, ?, ?, ?)", (patient_id, name, phone, appt_date, start, status))
        self.conn.commit()
        return cur.lastrowid

    def fill_day(self, appt_date):
        for start in cv._slot_starts():
            self.appt(appt_date, start, name="Busy", phone="9000000000")

    def counts(self):
        return {t: self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                for t in ("appointments", "proposals", "audit_log", "patients", "visits")}

    def session(self, wa=WA):
        return cv.load_session(self.conn, wa, self.clock)

    # -- driving a conversation -----------------------------------------------------
    def send(self, text=None, choice=None, wa=WA, minutes=0, msg_id=None):
        self.clock += timedelta(minutes=minutes)
        result = cv.handle_inbound(self.conn, wa, text or "", choice_id=choice, now=self.clock,
                                   msg_id=msg_id, intent_picker=self.picker)
        self.check_limits(result)
        return result

    def check_limits(self, result):
        """WhatsApp's interactive-message limits, on every reply of every test."""
        for r in result.replies:
            self.assertLessEqual(len(r.text), 1024)
            if r.buttons:
                self.assertLessEqual(len(r.buttons), 3)
                for _id, title in r.buttons:
                    self.assertLessEqual(len(title), 20, title)
                    self.assertTrue(cv.parse_choice(_id), _id)
            if r.rows:
                self.assertLessEqual(len(r.rows), 10)
                self.assertTrue(r.list_button and len(r.list_button) <= 20)
                for _id, title in r.rows:
                    self.assertLessEqual(len(title), 24, title)
                    self.assertTrue(cv.parse_choice(_id), _id)

    def assertReply(self, result, key, lang, index=0, **values):
        """The reply is exactly the fixed template `key` in `lang`."""
        self.assertGreater(len(result.replies), index, "no reply #{}".format(index))
        self.assertEqual(result.replies[index].text, ct.text(key, lang, **values))

    def fd(self, iso, lang):
        return notify.format_date(iso, lang)

    def ft(self, hhmm, lang):
        return notify.format_time(hhmm, lang)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class ParsingTests(unittest.TestCase):
    def test_choice_ids_round_trip_and_reject_garbage(self):
        self.assertEqual(cv.parse_choice(cv.menu_choice("book")), ("menu", "book"))
        self.assertEqual(cv.parse_choice(cv.slot_choice("2026-10-02", "09:15")), ("slot", "2026-10-02T09:15"))
        self.assertEqual(cv.parse_choice(cv.day_choice("2026-10-02")), ("day", "2026-10-02"))
        self.assertEqual(cv.parse_choice(cv.appt_choice(12)), ("appt", "12"))
        self.assertEqual(cv.parse_choice("confirm:yes"), ("confirm", "yes"))
        self.assertEqual(cv.parse_choice("confirm:change"), ("confirm", "change"))
        for bad in (None, "", "menu:delete", "slot:tomorrow", "appt:abc", "confirm:maybe", "x" * 300, "slot:2026-10-02"):
            self.assertIsNone(cv.parse_choice(bad), bad)

    def test_time_parsing(self):
        self.assertEqual(cv.parse_time("4 pm"), "16:00")
        self.assertEqual(cv.parse_time("9:15"), "09:15")
        self.assertEqual(cv.parse_time("शाम 4 बजे"), "16:00")
        self.assertEqual(cv.parse_time("shaam 4 baje"), "16:00")
        self.assertEqual(cv.parse_time("१० बजे"), "10:00")  # Devanagari digits
        self.assertIsNone(cv.parse_time("tomorrow"))
        self.assertIsNone(cv.parse_time("4"))                   # bare number only at a time question
        self.assertEqual(cv.parse_time("4", allow_bare_hour=True), "16:00")
        self.assertEqual(cv.parse_time("9", allow_bare_hour=True), "09:00")
        self.assertEqual(cv.parse_time("12", allow_bare_hour=True), "12:00")
        self.assertEqual(cv.parse_time("3", allow_bare_hour=True), "03:00")   # no valid reading -> literal, rejected later
        self.assertEqual(cv.parse_time("17", allow_bare_hour=True), "17:00")

    def test_day_parsing(self):
        today = NOW.date()
        self.assertEqual(cv.parse_day("tomorrow", today), TOMORROW)
        self.assertEqual(cv.parse_day("कल", today), TOMORROW)
        self.assertEqual(cv.parse_day("kal", today), TOMORROW)
        self.assertEqual(cv.parse_day("day after tomorrow", today), WED)
        self.assertEqual(cv.parse_day("parso", today), WED)
        self.assertEqual(cv.parse_day("परसों", today), WED)
        self.assertEqual(cv.parse_day("Friday", today), FRI)
        self.assertEqual(cv.parse_day("5/10", today), TODAY)
        self.assertEqual(cv.parse_day("9 अक्टूबर", today), FRI)
        self.assertEqual(cv.parse_day("९ अक्टूबर", today), FRI)
        self.assertEqual(cv.parse_day("7 oct", today), WED)
        self.assertIsNone(cv.parse_day("blorp", today))
        self.assertIsNone(cv.parse_day("12", today))
        self.assertEqual(cv.parse_day("12", today, allow_bare_day_of_month=True), "2026-10-12")
        self.assertEqual(cv.parse_day("3", today, allow_bare_day_of_month=True), "2026-11-03")  # already past this month

    def test_name_parsing(self):
        self.assertEqual(cv.parse_name("sunita devi"), "Sunita Devi")
        self.assertEqual(cv.parse_name("My name is Neeta Sharma"), "Neeta Sharma")
        self.assertEqual(cv.parse_name("mera naam Raju hai"), "Raju")
        self.assertEqual(cv.parse_name("सुनीता देवी"), "सुनीता देवी")
        self.assertEqual(cv.parse_name("मेरा नाम राजू है"), "राजू")
        for bad in ("", "9876543210", "Sunita 2", "yes", "hello", "cancel", "I have a fever", "talk to a person",
                    "one two three four five", "a" * 50):
            self.assertIsNone(cv.parse_name(bad), bad)

    def test_yes_no(self):
        for text in ("yes", "Yes!", "ok", "haan", "haan ji", "theek hai", "हाँ", "हां जी", "confirm", "कन्फर्म"):
            self.assertEqual(cv.parse_yes_no(text), "yes", text)
        for text in ("no", "nahi", "नहीं", "change", "badlein", "बदलें", "nahi nahi"):
            self.assertEqual(cv.parse_yes_no(text), "no", text)
        for text in ("maybe", "haan nahi", "4 pm", "tomorrow", ""):
            self.assertIsNone(cv.parse_yes_no(text), text)
        self.assertEqual(cv.parse_yes_no("cancel", goal="cancel"), "yes")
        self.assertEqual(cv.parse_yes_no("cancel", goal="book"), "no")

    def test_deterministic_intent(self):
        cases = {
            "book": ["I want to book an appointment", "mujhe appointment chahiye", "मुझे अपॉइंटमेंट चाहिए", "need a slot"],
            "cancel": ["cancel my appointment", "मेरी अपॉइंटमेंट रद्द करें", "mujhe appointment cancel karni hai", "nahi aa sakta"],
            "reschedule": ["reschedule", "change the date", "appointment ko doosre din karo", "अपॉइंटमेंट रीशेड्यूल करनी है"],
            "status": ["what is my token", "mera token kya hai", "मेरा टोकन क्या है", "how many people before me"],
            "greeting": ["hi", "Hello!", "namaste", "नमस्ते", "good morning", "help", "menu", "hi doctor"],
            "register": ["I want to register"],
        }
        for intent, texts in cases.items():
            for text in texts:
                self.assertEqual(cv.deterministic_intent(text), intent, text)
        for text in ("blorp", "4 pm", "tomorrow", "yes", "Sunita Devi", ""):
            self.assertIsNone(cv.deterministic_intent(text), text)

    def test_emergency_and_clinical_lists(self):
        for text in ("chest pain", "I can't breathe", "heavy bleeding", "he is unconscious", "behosh ho gaya",
                     "seizure", "there was an accident", "EMERGENCY!", "seene mein dard", "सीने में दर्द",
                     "सांस नहीं आ रही", "बहुत खून बह रहा है", "बेहोश हो गए", "मिर्गी का दौरा", "इमरजेंसी है",
                     "saans nahi aa rahi", "mirgi", "एक्सीडेंट हो गया"):
            self.assertTrue(cv.is_emergency(text), text)
        for text in ("I want to book", "kal 4 baje", "stroking the cat is fine", "accidentally typed"):
            self.assertFalse(cv.is_emergency(text), text)
        for text in ("I have a fever", "what dose should I take", "my test results", "bukhar hai", "बुखार है",
                     "dawai kaun si", "दवा बताइए", "500 mg tablet", "prescription please", "sugar high hai"):
            self.assertTrue(cv.is_clinical_question(text), text)
        for text in ("I want to book", "kal 4 baje", "cancel", "token"):
            self.assertFalse(cv.is_clinical_question(text), text)

    def test_every_template_exists_in_all_languages_and_titles_fit(self):
        for key, by_lang in ct.MSG.items():
            self.assertEqual(set(by_lang), set(ct.LANGUAGES), key)
        for key, by_lang in ct.BTN.items():
            for lang, title in by_lang.items():
                self.assertLessEqual(len(title), 20, (key, lang))
        self.assertEqual(ct.text("handoff_received", "en"), "Request received -- the clinic will confirm shortly.")
        self.assertEqual(
            ct.text("emergency", "en"),
            "If this is an emergency please call 112 (ambulance 108) or go to the nearest hospital now. The clinic has been alerted.")
        self.assertEqual(ct.text("clinical", "en"),
                         "We can't give medical advice on chat. Please call the clinic or book a consultation.")
        self.assertEqual(ct.text("escalate", "en"), "A member of our team will reply to you shortly.")


# ---------------------------------------------------------------------------
# Menu and greeting
# ---------------------------------------------------------------------------

class MenuTests(ConvCase):
    def test_greeting_shows_the_menu_with_three_buttons_in_the_patients_language(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                r = self.send(w["hello"])
                self.assertEqual(len(r.replies), 1)
                self.assertEqual(r.replies[0].text, ct.text("menu", lang, greet=ct.greet(lang)))
                self.assertEqual([i for i, _ in r.replies[0].buttons],
                                 ["menu:book", "menu:reschedule", "menu:cancel"])
                self.assertEqual([t for _, t in r.replies[0].buttons],
                                 [ct.button("book", lang), ct.button("reschedule", lang), ct.button("cancel", lang)])
                self.assertIsNone(r.handoff)
                self.assertIsNone(r.flag)

    def test_registered_patient_is_greeted_by_first_name(self):
        self.patient("Sunita Devi", "9876543210")
        r = self.send("hi")
        self.assertIn("Hello Sunita,", r.replies[0].text)

    def test_menu_buttons_start_the_flows(self):
        self.patient()
        r = self.send(choice="menu:book")
        self.assertEqual(self.session()["goal"], "book")
        self.assertTrue(r.replies[0].buttons[0][0].startswith("day:"))
        r = self.send(choice="menu:cancel")                     # no appointment -> offers to book
        self.assertEqual(r.replies[0].text, ct.text("no_appt", "en"))
        self.assertEqual(r.replies[0].buttons, [("menu:book", "Book appointment")])
        self.assertIsNone(self.session()["goal"])

    def test_unclear_message_with_unclear_llm_gets_the_menu_then_escalates(self):
        r = self.send("blorp")
        self.assertEqual(r.replies[0].text, ct.text("menu_unclear", "en"))
        self.assertEqual(len(r.replies[0].buttons), 3)
        self.assertEqual(self.picker.calls, ["blorp"])
        r = self.send("blorp again")
        self.assertEqual(r.escalate, "confusion")
        self.assertEqual(r.flag, "escalation")
        self.assertEqual(r.replies[0].text, ct.text("escalate", "en"))

    def test_thanks_gets_a_polite_fixed_reply(self):
        r = self.send("thanks")
        self.assertEqual(r.replies[0].text, ct.text("thanks", "en"))
        self.assertIsNone(r.flag)
        self.assertEqual(self.picker.calls, [])


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------

class BookFlowTests(ConvCase):
    def test_unregistered_caller_full_flow_in_every_language(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                wa = WA_FOR[lang]
                before = self.counts()
                r = self.send(w["book"], wa=wa)
                self.assertReply(r, "ask_name", lang)
                self.assertIsNone(r.replies[0].buttons)
                self.assertEqual(self.session(wa)["step"], "name")

                r = self.send(w["name"], wa=wa)
                self.assertEqual(r.replies[0].text[:20], ct.text("ask_day", lang)[:20])
                self.assertEqual([i for i, _ in r.replies[0].buttons],
                                 ["day:" + TODAY, "day:" + TOMORROW, "day:" + WED])

                r = self.send(w["tomorrow"], wa=wa)
                reply = r.replies[0]
                self.assertEqual(reply.text, ct.text("offer_slots", lang, date=self.fd(TOMORROW, lang)))
                self.assertEqual(len(reply.rows), 5)
                self.assertEqual(reply.rows[0][0], "slot:{}T09:00".format(TOMORROW))
                self.assertTrue(all(i.startswith("slot:" + TOMORROW) for i, _ in reply.rows))
                self.assertEqual(reply.list_button, ct.button("choose_time", lang))

                r = self.send(w["t4pm"], wa=wa)
                self.assertReply(r, "confirm_book", lang, name=w["name"], date=self.fd(TOMORROW, lang),
                                 time=self.ft("16:00", lang))
                self.assertEqual(r.replies[0].buttons,
                                 [("confirm:yes", ct.button("confirm", lang)), ("confirm:change", ct.button("change", lang))])
                self.assertIsNone(r.handoff)

                r = self.send(choice="confirm:yes", wa=wa, msg_id=77)
                self.assertReply(r, "handoff_received", lang)
                self.assertEqual(len(r.replies), 1)
                h = r.handoff
                self.assertEqual(h.intent, "book_appointment")
                self.assertEqual(h.slots["appt_date"], TOMORROW)
                self.assertEqual(h.slots["start_time"], "16:00")
                self.assertEqual(h.slots["patient_name"], w["name"])
                self.assertEqual(h.slots["patient_phone"], wa[-10:])
                self.assertIsNone(h.slots["patient_id"])
                self.assertIn("WhatsApp conversation", h.slots["notes"])
                self.assertEqual(h.slots["via"], "conversation")
                self.assertTrue(h.slots["agent_note"])
                self.assertIsNone(r.flag)
                self.assertIsNone(self.session(wa)["goal"])
                # The agent proposed -- it wrote nothing.
                self.assertEqual(self.counts(), before)
                # ...except a hold on the slot for this patient, linked to the inbox row.
                hold = self.conn.execute("SELECT * FROM slot_holds").fetchone()
                self.assertEqual((hold["wa_id"], hold["appt_date"], hold["start_time"], hold["wa_message_id"]),
                                 (wa, TOMORROW, "16:00", 77))

    def test_registered_patient_is_not_asked_for_a_name(self):
        pid = self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.conn.execute("DELETE FROM slot_holds")
                r = self.send(w["book"])
                self.assertEqual(r.replies[0].text[:15], ct.text("ask_day", lang)[:15])
                self.send(w["tomorrow"])
                self.send(w["t4pm"])
                r = self.send(choice="confirm:yes")
                self.assertEqual(r.handoff.slots["patient_id"], pid)
                self.assertIsNone(r.handoff.slots["patient_name"])
                self.assertIsNone(r.handoff.slots["patient_phone"])
                self.assertEqual(r.patient_id, pid)

    def test_everything_in_the_first_message_goes_straight_to_the_summary(self):
        self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                r = self.send(w["book_all"])
                self.assertReply(r, "confirm_book", lang, name="Sunita Devi", date=self.fd(TOMORROW, lang),
                                 time=self.ft("16:00", lang))

    def test_tapping_a_day_then_a_time_choice(self):
        self.patient()
        self.send(choice="menu:book")
        r = self.send(choice="day:" + TOMORROW)
        self.assertEqual(r.replies[0].rows[0][0], "slot:{}T09:00".format(TOMORROW))
        r = self.send(choice="slot:{}T11:00".format(TOMORROW))
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")
        self.assertIn("11:00 AM", r.replies[0].text)
        r = self.send(choice="confirm:yes")
        self.assertEqual(r.handoff.slots["start_time"], "11:00")

    def test_a_bare_hour_is_read_as_the_only_valid_am_pm_reading(self):
        self.patient()
        self.send("book kal")
        r = self.send("4")
        self.assertIn("4:00 PM", r.replies[0].text)
        self.assertEqual(self.session()["slots"]["start_time"], "16:00")

    def test_change_goes_back_to_the_day_question(self):
        self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.send(w["book_all"])
                r = self.send(choice="confirm:change")
                self.assertEqual(r.replies[0].text[:20], ct.text("ask_day_changed", lang)[:20])
                self.assertNotIn("appt_date", self.session()["slots"])
                self.send(w["book_all"])
                r = self.send(w["no"])      # a typed "no"/"change" is the same
                self.assertEqual(r.replies[0].text[:20], ct.text("ask_day_changed", lang)[:20])

    def test_typed_yes_confirms(self):
        self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.conn.execute("DELETE FROM slot_holds")
                self.send(w["book_all"])
                r = self.send(w["yes"])
                self.assertEqual(r.handoff.intent, "book_appointment")

    def test_a_new_time_typed_at_the_summary_changes_the_request(self):
        self.patient()
        self.send("book tomorrow 4 pm")
        r = self.send("5:30 pm")
        self.assertIn("5:30 PM", r.replies[0].text)
        self.assertEqual(self.send(choice="confirm:yes").handoff.slots["start_time"], "17:30")

    def test_a_day_and_time_can_be_given_together_at_the_day_question(self):
        self.patient()
        self.send("book")
        r = self.send("friday 11 am")
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")
        self.assertIn(self.fd(FRI, "en"), r.replies[0].text)

    def test_offer_is_buttons_when_three_or_fewer_times_remain(self):
        self.patient()
        keep = {"16:00", "17:00", "19:30"}
        for start in cv._slot_starts():
            if start not in keep:
                self.appt(TOMORROW, start, name="Busy", phone="9000000000")
        self.send("book")
        r = self.send("tomorrow")
        self.assertEqual([i for i, _ in r.replies[0].buttons],
                         ["slot:{}T{}".format(TOMORROW, t) for t in sorted(keep)])
        self.assertIsNone(r.replies[0].rows)

    def test_full_day_offers_the_next_day_with_free_slots(self):
        self.patient()
        self.fill_day(TOMORROW)
        self.send("book")
        r = self.send("tomorrow")
        self.assertEqual(r.replies[0].text,
                         ct.text("offer_next_day", "en", date=self.fd(TOMORROW, "en"), next_date=self.fd(WED, "en")))
        self.assertTrue(r.replies[0].rows[0][0].startswith("slot:" + WED))
        self.assertEqual(self.session()["slots"]["appt_date"], WED)
        r = self.send("10 am")
        self.assertIn(self.fd(WED, "en"), r.replies[0].text)

    def test_today_only_offers_times_that_have_not_passed(self):
        self.patient()
        self.send("book today")
        times = [t for _, t in self.send("today").replies[0].rows]
        self.assertNotIn("9:00 AM", times)
        self.assertEqual(times[0], "10:30 AM")

    def test_a_fully_booked_horizon_escalates(self):
        self.patient()
        for offset in range(0, cv.BOOKING_HORIZON_DAYS + 1):
            self.fill_day((NOW.date() + timedelta(days=offset)).isoformat())
        r = self.send("book")
        self.assertEqual(r.escalate, "no_availability")
        self.assertEqual(r.replies[0].text, ct.text("no_availability", "en", days=cv.BOOKING_HORIZON_DAYS))


class BookValidationTests(ConvCase):
    def test_past_date_is_rejected_and_the_day_is_asked_again(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                wa = WA_FOR[lang]
                self.send(w["book"], wa=wa)
                self.send(w["name"], wa=wa)
                r = self.send(w["past"], wa=wa)
                self.assertReply(r, "date_past", lang)
                self.assertEqual(self.session(wa)["step"], "day")
                self.assertNotIn("appt_date", self.session(wa)["slots"])
                self.assertTrue(r.replies[0].buttons[0][0].startswith("day:"))

    def test_date_beyond_the_horizon_is_rejected(self):
        self.patient()
        self.send("book")
        r = self.send("1/1/2027")
        self.assertEqual(r.replies[0].text, ct.text("date_far", "en", days=cv.BOOKING_HORIZON_DAYS))

    def test_out_of_hours_time_is_rejected_with_the_clinic_hours(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                wa = WA_FOR[lang]
                self.send(w["book"], wa=wa)
                self.send(w["name"], wa=wa)
                self.send(w["tomorrow"], wa=wa)
                r = self.send(w["t11pm"], wa=wa)
                hours = ct.text("and", lang).join(
                    "{}-{}".format(self.ft(a, lang), self.ft(b, lang)) for a, b in cv.scheduling.CLINIC_HOURS)
                self.assertEqual(r.replies[0].text, ct.text("time_closed", lang, hours=hours, date=""))
                self.assertEqual(len(r.replies[0].rows), 5)           # the list is offered again
                self.assertNotIn("start_time", self.session(wa)["slots"])
                self.assertEqual(self.session(wa)["step"], "time")

    def test_lunch_gap_and_before_opening_are_closed(self):
        self.patient()
        self.send("book tomorrow")
        for text in ("2 pm", "8 am", "8 pm", "6:00 am"):
            r = self.send(text)
            self.assertIn("The clinic is open", r.replies[0].text, text)

    def test_a_time_earlier_today_is_rejected_as_past(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                wa = WA_FOR[lang]
                self.send(w["book"], wa=wa)
                self.send(w["name"], wa=wa)
                self.send("5/10", wa=wa)       # today
                r = self.send(w["t9am"], wa=wa)
                self.assertReply(r, "time_past", lang)

    def test_a_taken_slot_is_not_available(self):
        self.patient()
        self.appt(TOMORROW, "16:00", name="Other", phone="9000000001")
        self.send("book tomorrow")
        r = self.send("4 pm")
        self.assertEqual(r.replies[0].text, ct.text("time_taken", "en", time="4:00 PM", date=self.fd(TOMORROW, "en")))
        self.assertNotIn("slot:{}T16:00".format(TOMORROW), [i for i, _ in r.replies[0].rows])

    def test_off_grid_time_inside_hours_is_not_available(self):
        self.patient()
        self.send("book tomorrow")
        r = self.send("9:20 am")
        self.assertIn("is not available", r.replies[0].text)

    def test_slot_booked_by_someone_else_after_the_offer_is_caught_at_confirm(self):
        self.patient()
        self.send("book tomorrow 4 pm")                         # summary shown
        self.appt(TOMORROW, "16:00", name="Fast", phone="9000000002")
        r = self.send(choice="confirm:yes")
        self.assertIsNone(r.handoff)
        # Intentional change with automatic booking: the patient had already
        # confirmed this exact time, so the fixed "just taken" template (not the
        # bare "not available") is used, followed by fresh times.
        self.assertIn("was just taken", r.replies[0].text)
        self.assertEqual(r.replies[0].text, ct.text("slot_just_taken", "en", time="4:00 PM", date="Tuesday, 6 Oct 2026"))
        self.assertTrue(r.replies[0].rows or r.replies[0].buttons)
        self.assertEqual([a["event"] for a in r.activity], ["conflict"])
        self.assertEqual(self.session()["step"], "time")

    def test_per_person_cap_counts_appointments_and_open_requests(self):
        pid = self.patient()
        for start in ("09:00", "09:15"):
            self.appt(TOMORROW, start, patient_id=pid)
        r = self.send("book")
        self.assertEqual(self.session()["goal"], "book")        # 2 < 3: allowed
        self.conn.execute("DELETE FROM wa_sessions")
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, intent, slots_json, status) "
            "VALUES ('w1', ?, 'text', 'x', 'book_appointment', '{}', 'classified')", (WA,))
        r = self.send("book")
        self.assertEqual(r.replies[0].text, ct.text("cap_reached", "en", n=3))
        self.assertIsNone(self.session()["goal"])

    def test_cap_for_an_unregistered_booker_uses_the_phone_on_the_appointment(self):
        for start in ("09:00", "09:15", "09:30"):
            self.appt(TOMORROW, start, name="Raju", phone="9876543210")
        r = self.send("book")
        self.assertEqual(r.replies[0].text, ct.text("cap_reached", "en", n=3))

    def test_cancelled_appointments_do_not_count(self):
        pid = self.patient()
        for start in ("09:00", "09:15", "09:30"):
            self.appt(TOMORROW, start, patient_id=pid, status="cancelled")
        self.send("book")
        self.assertEqual(self.session()["goal"], "book")


# ---------------------------------------------------------------------------
# Reschedule and cancel
# ---------------------------------------------------------------------------

class RescheduleFlowTests(ConvCase):
    def test_single_appointment_flow_in_every_language(self):
        pid = self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.conn.execute("DELETE FROM slot_holds")
                self.conn.execute("DELETE FROM appointments")
                aid = self.appt(TOMORROW, "09:00", patient_id=pid)
                before = self.counts()
                r = self.send(w["resched"])
                self.assertEqual(r.replies[0].text, ct.text("ask_day_resched", lang, date=self.fd(TOMORROW, lang),
                                                             time=self.ft("09:00", lang)))
                self.assertEqual(self.session()["goal"], "reschedule")
                r = self.send(w["friday"])
                self.assertEqual(r.replies[0].text, ct.text("offer_slots", lang, date=self.fd(FRI, lang)))
                r = self.send(w["t4pm"])
                self.assertReply(r, "confirm_reschedule", lang, old_date=self.fd(TOMORROW, lang),
                                 old_time=self.ft("09:00", lang), date=self.fd(FRI, lang), time=self.ft("16:00", lang))
                r = self.send(choice="confirm:yes", msg_id=5)
                self.assertReply(r, "handoff_received", lang)
                self.assertEqual(r.handoff.intent, "reschedule_appointment")
                self.assertEqual(r.handoff.slots["appointment_id"], aid)
                self.assertEqual((r.handoff.slots["appt_date"], r.handoff.slots["start_time"]), (FRI, "16:00"))
                self.assertEqual(r.handoff.slots["via"], "conversation")
                self.assertEqual(self.counts(), before)
                hold = self.conn.execute("SELECT * FROM slot_holds").fetchone()
                self.assertEqual((hold["appt_date"], hold["start_time"], hold["wa_message_id"]), (FRI, "16:00", 5))

    def test_several_appointments_ask_which_one_from_a_list(self):
        pid = self.patient()
        a1 = self.appt(TOMORROW, "09:00", patient_id=pid)
        a2 = self.appt(FRI, "10:00", patient_id=pid)
        r = self.send("reschedule my appointment")
        self.assertEqual(r.replies[0].text, ct.text("which_appt", "en"))
        self.assertEqual([i for i, _ in r.replies[0].rows], ["appt:{}".format(a1), "appt:{}".format(a2)])
        self.assertEqual(r.replies[0].rows[0][1], "Tue 6 Oct, 9:00 AM")
        r = self.send(choice="appt:{}".format(a2))
        self.assertIn(self.fd(FRI, "en"), r.replies[0].text)
        self.send("wednesday")
        r = self.send("11 am")
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")
        self.assertEqual(self.send(choice="confirm:yes").handoff.slots["appointment_id"], a2)

    def test_the_date_in_the_first_message_is_remembered_across_the_which_question(self):
        pid = self.patient()
        self.appt(TOMORROW, "09:00", patient_id=pid)
        a2 = self.appt(FRI, "10:00", patient_id=pid)
        self.send("reschedule my appointment to wednesday 11 am")
        r = self.send(choice="appt:{}".format(a2))
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")
        self.assertIn("11:00 AM", r.replies[0].text)

    def test_which_can_be_answered_with_the_appointments_date(self):
        pid = self.patient()
        self.appt(TOMORROW, "09:00", patient_id=pid)
        a2 = self.appt(FRI, "10:00", patient_id=pid)
        self.send("reschedule")
        self.send("friday")
        self.assertEqual(self.session()["slots"]["appointment_id"], a2)

    def test_no_appointment_offers_to_book(self):
        self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                r = self.send(w["resched"])
                self.assertReply(r, "no_appt", lang)
                self.assertEqual(r.replies[0].buttons, [("menu:book", ct.button("book", lang))])

    def test_a_past_appointment_today_cannot_be_rescheduled(self):
        pid = self.patient()
        self.appt(TODAY, "09:00", patient_id=pid)             # earlier today, now is 10:00
        r = self.send("reschedule")
        self.assertEqual(r.replies[0].text, ct.text("no_appt", "en"))

    def test_a_second_request_for_the_same_appointment_is_not_started(self):
        pid = self.patient()
        aid = self.appt(TOMORROW, "09:00", patient_id=pid)
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, intent, slots_json, status) "
            "VALUES ('w9', ?, 'text', 'x', 'cancel_appointment', ?, 'classified')", (WA, json.dumps({"appointment_id": aid})))
        r = self.send("cancel my appointment")
        self.assertEqual(r.replies[0].text, ct.text("dup_request", "en"))
        self.assertIsNone(self.session()["goal"])


class CancelFlowTests(ConvCase):
    def test_cancel_yes_and_no_in_every_language(self):
        pid = self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.conn.execute("DELETE FROM appointments")
                aid = self.appt(TOMORROW, "09:15", patient_id=pid)
                before = self.counts()
                r = self.send(w["cancel"])
                self.assertReply(r, "confirm_cancel", lang, date=self.fd(TOMORROW, lang), time=self.ft("09:15", lang))
                self.assertEqual(r.replies[0].buttons,
                                 [("confirm:yes", ct.button("yes", lang)), ("confirm:no", ct.button("no", lang))])
                r = self.send(choice="confirm:yes")
                self.assertReply(r, "handoff_received", lang)
                self.assertEqual(r.handoff.intent, "cancel_appointment")
                self.assertEqual(r.handoff.slots["appointment_id"], aid)
                self.assertEqual(self.counts(), before)                 # nothing cancelled by the agent
                self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0], "booked")
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0], 0)

                # ...and the other answer.
                self.conn.execute("DELETE FROM wa_sessions")
                self.send(w["cancel"])
                r = self.send(choice="confirm:no")
                self.assertReply(r, "nothing_cancelled", lang)
                self.assertIsNone(r.handoff)
                self.assertIsNone(self.session()["goal"])

    def test_typed_yes_and_no_answer_the_confirmation(self):
        pid = self.patient()
        self.appt(TOMORROW, "09:15", patient_id=pid)
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.send(w["cancel"])
                self.assertEqual(self.send(w["yes"]).handoff.intent, "cancel_appointment")
                self.conn.execute("DELETE FROM wa_sessions")
                self.send(w["cancel"])
                self.assertIsNone(self.send(w["no"]).handoff)

    def test_several_appointments_ask_which_then_confirm(self):
        pid = self.patient()
        a1 = self.appt(TOMORROW, "09:00", patient_id=pid)
        a2 = self.appt(FRI, "10:00", patient_id=pid)
        r = self.send("cancel my appointment")
        self.assertEqual(len(r.replies[0].rows), 2)
        r = self.send(choice="appt:{}".format(a1))
        self.assertIn(self.fd(TOMORROW, "en"), r.replies[0].text)
        r = self.send("cancel")                                  # a bare "cancel" at the cancel question means yes
        self.assertEqual(r.handoff.slots["appointment_id"], a1)

    def test_garbage_at_the_confirmation_reasks_with_the_buttons(self):
        pid = self.patient()
        self.appt(TOMORROW, "09:15", patient_id=pid)
        self.send("cancel my appointment")
        r = self.send("blorp")
        self.assertEqual(r.replies[0].text, ct.text("reask_yesno", "en"))
        self.assertEqual(len(r.replies[0].buttons), 2)

    def test_unregistered_booker_can_cancel_their_own_phone_booking(self):
        aid = self.appt(TOMORROW, "09:15", name="Raju", phone="9876543210")
        self.send("cancel my appointment")
        r = self.send(choice="confirm:yes")
        self.assertEqual(r.handoff.slots["appointment_id"], aid)
        self.assertIsNone(r.handoff.patient_id)

    def test_cancel_with_nothing_booked(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                r = self.send(w["cancel"])
                self.assertReply(r, "no_appt", lang)


class OnlyOwnAppointmentsTests(ConvCase):
    def test_a_patient_can_never_pick_someone_elses_appointment(self):
        a = self.patient("Anita", "9876543210")
        b = self.patient("Bhavna", "9111122223")
        a_appt = self.appt(TOMORROW, "09:00", patient_id=a)
        b1 = self.appt(TOMORROW, "09:15", patient_id=b)
        b2 = self.appt(FRI, "09:30", patient_id=b)
        r = self.send("cancel my appointment", wa=WA2)
        self.assertEqual(sorted(i for i, _ in r.replies[0].rows), sorted(["appt:{}".format(b1), "appt:{}".format(b2)]))
        r = self.send(choice="appt:{}".format(a_appt), wa=WA2)           # forged / guessed id
        self.assertIsNone(r.handoff)
        self.assertEqual(self.session(WA2)["step"], "which")             # still asking
        self.assertNotIn("appointment_id", self.session(WA2)["slots"])
        self.assertEqual(r.replies[0].text, ct.text("which_appt", "en"))
        r = self.send(choice="appt:{}".format(b1), wa=WA2)
        self.send(choice="confirm:yes", wa=WA2)
        self.assertEqual(self.session(WA2)["step"], "submitted")

    def test_a_number_with_no_appointments_gets_nothing_to_act_on(self):
        a = self.patient("Anita", "9876543210")
        self.appt(TOMORROW, "09:00", patient_id=a)
        for text in ("cancel my appointment", "reschedule my appointment"):
            r = self.send(text, wa=WA2)
            self.assertEqual(r.replies[0].text, ct.text("no_appt", "en"))
        r = self.send("what is my token", wa=WA2)
        self.assertEqual(r.replies[0].text, ct.text("status_none", "en"))
        self.assertNotEqual(r.replies[0].kind, "status")

    def test_status_never_names_another_persons_appointment(self):
        a = self.patient("Anita", "9876543210")
        a_appt = self.appt(TOMORROW, "09:00", patient_id=a)
        r = self.send("what is my token", wa=WA)
        self.assertEqual((r.replies[0].kind, r.replies[0].appointment_id), ("status", a_appt))
        r = self.send("what is my token", wa=WA2)
        self.assertEqual(r.replies[0].kind, "message")

    def test_a_stale_appointment_choice_is_ignored_when_no_flow_is_active(self):
        a = self.patient()
        aid = self.appt(TOMORROW, "09:00", patient_id=a)
        r = self.send(choice="appt:{}".format(aid))
        self.assertIsNone(r.handoff)
        self.assertEqual(r.replies[0].text, ct.text("session_expired", "en"))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class StatusTests(ConvCase):
    def test_status_with_an_appointment_is_a_read_only_status_reply(self):
        pid = self.patient()
        aid = self.appt(TOMORROW, "09:00", patient_id=pid)
        before = self.counts()
        for lang, w in SAY.items():
            with self.subTest(lang):
                r = self.send(w["status"])
                self.assertEqual(len(r.replies), 1)
                self.assertEqual((r.replies[0].kind, r.replies[0].appointment_id), ("status", aid))
                self.assertIsNone(r.handoff)
                self.assertIsNone(r.flag)
        self.assertEqual(self.counts(), before)

    def test_status_with_no_appointment_offers_to_book(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                r = self.send(w["status"])
                self.assertReply(r, "status_none", lang)
                self.assertEqual(r.replies[0].buttons, [("menu:book", ct.button("book", lang))])

    def test_menu_status_choice(self):
        pid = self.patient()
        aid = self.appt(TOMORROW, "09:00", patient_id=pid)
        r = self.send(choice="menu:status")
        self.assertEqual(r.replies[0].appointment_id, aid)


# ---------------------------------------------------------------------------
# Context switch, confusion, expiry
# ---------------------------------------------------------------------------

class ContextSwitchTests(ConvCase):
    def test_cancel_in_the_middle_of_a_booking_switches_goals(self):
        pid = self.patient()
        aid = self.appt(FRI, "10:00", patient_id=pid)
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                self.send(w["book"])
                self.send(w["tomorrow"])
                self.assertEqual(self.session()["step"], "time")
                r = self.send(w["cancel"])
                self.assertReply(r, "confirm_cancel", lang, date=self.fd(FRI, lang), time=self.ft("10:00", lang))
                s = self.session()
                self.assertEqual((s["goal"], s["step"]), ("cancel", "confirm"))
                self.assertNotIn("appt_date", s["slots"])           # the half-made booking was dropped

    def test_status_mid_flow_answers_and_ends_the_flow(self):
        pid = self.patient()
        aid = self.appt(FRI, "10:00", patient_id=pid)
        self.send("book")
        r = self.send("what is my token")
        self.assertEqual(r.replies[0].kind, "status")
        self.assertIsNone(self.session()["goal"])

    def test_book_during_cancel_switches(self):
        pid = self.patient()
        self.appt(FRI, "10:00", patient_id=pid)
        self.send("cancel my appointment")
        r = self.send("actually I want to book an appointment")
        self.assertEqual(self.session()["goal"], "book")
        self.assertEqual(r.replies[0].buttons[0][0].split(":")[0], "day")

    def test_greeting_mid_flow_restarts_at_the_menu(self):
        self.patient()
        self.send("book")
        r = self.send("hi")
        self.assertEqual(r.replies[0].buttons[0][0], "menu:book")
        self.assertIsNone(self.session()["goal"])

    def test_cancel_word_at_a_booking_summary_means_no_not_a_switch(self):
        self.patient()
        self.send("book tomorrow 4 pm")
        r = self.send("cancel")
        self.assertEqual(self.session()["goal"], "book")
        self.assertEqual(r.replies[0].text[:20], ct.text("ask_day_changed", "en")[:20])

    def test_same_goal_words_do_not_restart_the_flow(self):
        self.patient()
        self.send("book")
        self.send("tomorrow")
        self.send("book 4 pm")
        self.assertEqual(self.session()["slots"]["start_time"], "16:00")


class ConfusionTests(ConvCase):
    def test_unparseable_reply_is_reasked_once_with_an_example_then_escalates(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                wa = WA_FOR[lang]
                self.send(w["book"], wa=wa)
                self.send(w["name"], wa=wa)
                r = self.send(w["nonsense"], wa=wa)
                self.assertEqual(r.replies[0].text, ct.text("ask_day_retry", lang))
                self.assertEqual(self.session(wa)["confusion_count"], 1)
                self.assertIsNone(r.flag)
                r = self.send(w["nonsense"], wa=wa)
                self.assertReply(r, "escalate", lang)
                self.assertEqual((r.escalate, r.flag), ("confusion", "escalation"))
                self.assertIsNone(self.session(wa)["goal"])
                self.assertEqual(self.session(wa)["confusion_count"], 0)

    def test_a_good_answer_between_confused_turns_resets_the_count(self):
        self.patient()
        self.send("book")
        self.send("blorp")
        self.send("tomorrow")
        self.assertEqual(self.session()["confusion_count"], 0)
        r = self.send("blorp")
        self.assertIsNone(r.escalate)
        self.assertEqual(r.replies[0].text, ct.text("offer_retry", "en", date=""))  # re-asked with the list

    def test_garbage_at_each_question_reasks_that_question(self):
        self.send("book")
        self.assertEqual(self.send("1234").replies[0].text, ct.text("ask_name_retry", "en"))
        self.send("Sunita")
        self.send("tomorrow")
        r = self.send("blorp")
        self.assertEqual(r.replies[0].text, ct.text("offer_retry", "en"))
        self.assertEqual(len(r.replies[0].rows), 5)

    def test_a_time_given_at_the_day_question_counts_as_confused_but_is_remembered(self):
        self.patient()
        self.send("book")
        r = self.send("4 pm")
        self.assertEqual(r.replies[0].text[:20], ct.text("ask_day_retry", "en")[:20])
        self.assertEqual(self.session()["slots"]["start_time"], "16:00")
        r = self.send("tomorrow")
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")


class ExpiryTests(ConvCase):
    def test_idle_session_expires_after_30_minutes(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                wa = WA_FOR[lang]
                self.send(w["book"], wa=wa)
                self.send(w["name"], wa=wa)
                self.send(w["tomorrow"], wa=wa)
                self.assertEqual(self.session(wa)["step"], "time")
                r = self.send(w["t4pm"], wa=wa, minutes=31)
                self.assertReply(r, "session_expired", lang)
                self.assertEqual([i for i, _ in r.replies[0].buttons], ["menu:book", "menu:reschedule", "menu:cancel"])
                s = self.session(wa)
                self.assertEqual((s["goal"], s["slots"]), (None, {}))

    def test_a_tapped_slot_after_expiry_is_not_acted_on(self):
        self.patient()
        self.send("book tomorrow")
        r = self.send(choice="slot:{}T16:00".format(TOMORROW), minutes=31)
        self.assertIsNone(r.handoff)
        self.assertEqual(r.replies[0].text, ct.text("session_expired", "en"))

    def test_29_minutes_is_still_alive(self):
        self.patient()
        self.send("book tomorrow")
        r = self.send("4 pm", minutes=29)
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")

    def test_activity_extends_the_session(self):
        self.patient()
        self.send("book")
        self.send("tomorrow", minutes=20)
        r = self.send("4 pm", minutes=20)       # 40 minutes after the start, 20 after the last message
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")

    def test_a_fresh_intent_after_expiry_just_starts_a_new_flow(self):
        self.patient()
        self.send("book tomorrow")
        r = self.send("book friday", minutes=45)
        self.assertEqual(self.session()["goal"], "book")
        self.assertIn(self.fd(FRI, "en"), r.replies[0].text)

    def test_mode_persists_across_expiry(self):
        cv.set_mode(self.conn, WA, "human", now=self.clock)
        self.send("hello", minutes=120)
        self.assertEqual(cv.get_mode(self.conn, WA), "human")

    def test_submitted_request_is_remembered_for_a_double_tap(self):
        self.patient()
        self.send("book tomorrow 4 pm")
        self.send(choice="confirm:yes")
        r = self.send(choice="confirm:yes")
        self.assertEqual(r.replies[0].text, ct.text("already_submitted", "en"))
        self.assertIsNone(r.handoff)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0], 1)


# ---------------------------------------------------------------------------
# Slot holds
# ---------------------------------------------------------------------------

class SlotHoldTests(ConvCase):
    def confirm_for(self, wa, name, msg_id):
        self.send("book tomorrow 4 pm", wa=wa)
        self.send(name, wa=wa)
        self.send(choice="confirm:yes", wa=wa, msg_id=msg_id)

    def test_a_held_slot_is_not_offered_to_or_accepted_from_another_sender(self):
        self.send("book tomorrow 4 pm", wa=WA)
        self.send("Anita", wa=WA)
        r = self.send(choice="confirm:yes", wa=WA, msg_id=11)
        self.assertEqual(r.handoff.slots["start_time"], "16:00")

        self.assertNotIn("16:00", cv.free_times(self.conn, TOMORROW, self.clock, for_wa_id=WA2))
        self.assertIn("16:00", cv.free_times(self.conn, TOMORROW, self.clock, for_wa_id=WA))   # own hold is not hidden

        self.send("book tomorrow", wa=WA2)
        self.send("Bhavna", wa=WA2)
        r = self.send("4 pm", wa=WA2)
        self.assertEqual(r.replies[0].text, ct.text("time_taken", "en", time="4:00 PM", date=self.fd(TOMORROW, "en")))
        offered = [i for i, _ in r.replies[0].rows or r.replies[0].buttons]
        self.assertNotIn("slot:{}T16:00".format(TOMORROW), offered)
        self.assertEqual(self.session(WA2)["step"], "time")

    def test_hold_is_released_when_staff_resolve_the_request(self):
        self.confirm_for(WA, "Anita", 11)
        self.assertNotIn("16:00", cv.free_times(self.conn, TOMORROW, self.clock, for_wa_id=WA2))
        self.assertEqual(cv.release_holds(self.conn, 11), 1)
        self.assertIn("16:00", cv.free_times(self.conn, TOMORROW, self.clock, for_wa_id=WA2))
        self.assertEqual(cv.release_holds(self.conn, 11), 0)
        self.assertEqual(cv.release_holds(self.conn, None), 0)

    def test_hold_expires_after_two_hours(self):
        self.confirm_for(WA, "Anita", 11)
        self.assertNotIn("16:00", cv.free_times(self.conn, TOMORROW, self.clock + timedelta(minutes=119), for_wa_id=WA2))
        self.assertIn("16:00", cv.free_times(self.conn, TOMORROW, self.clock + timedelta(hours=2), for_wa_id=WA2))
        self.send("hello", wa=WA2, minutes=130)       # any turn purges expired holds
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM slot_holds").fetchone()[0], 0)

    def test_holds_only_hide_slots_they_never_block_a_confirm_time_booking(self):
        """The hold is an offer-time convenience; scheduling.is_slot_free is untouched."""
        self.confirm_for(WA, "Anita", 11)
        self.assertTrue(cv.scheduling.is_slot_free(self.conn, TOMORROW, "16:00", 15))

    def test_a_held_slot_does_not_make_a_day_look_available(self):
        for start in cv._slot_starts():
            if start != "16:00":
                self.appt(TOMORROW, start, name="Busy", phone="9000000000")
        self.confirm_for(WA, "Anita", 11)
        self.send("book tomorrow", wa=WA2)
        r = self.send("Bhavna", wa=WA2)
        self.assertIn(self.fd(WED, "en"), r.replies[0].text)

    def test_two_senders_do_not_share_sessions(self):
        self.send("book", wa=WA)
        self.send("hello", wa=WA2)
        self.assertEqual(self.session(WA)["goal"], "book")
        self.assertIsNone(self.session(WA2)["goal"])


# ---------------------------------------------------------------------------
# Safety: emergency, clinical, escalation, takeover, rate limit
# ---------------------------------------------------------------------------

class EmergencyTests(ConvCase):
    def test_emergency_in_every_language(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                r = self.send(w["emergency"])
                self.assertReply(r, "emergency", lang)
                self.assertEqual(len(r.replies), 1)
                self.assertTrue(r.emergency)
                self.assertEqual(r.flag, "emergency")
                self.assertIsNone(r.handoff)

    def test_emergency_text_is_the_specified_fixed_message(self):
        r = self.send("I have chest pain")
        self.assertEqual(
            r.replies[0].text,
            "If this is an emergency please call 112 (ambulance 108) or go to the nearest hospital now. The clinic has been alerted.")

    def test_emergency_runs_first_even_mid_flow_and_leaves_the_flow_intact(self):
        self.patient()
        self.send("book")
        self.send("tomorrow")
        r = self.send("wait my father is unconscious")
        self.assertTrue(r.emergency)
        s = self.session()
        self.assertEqual((s["goal"], s["step"]), ("book", "time"))

    def test_emergency_wins_over_a_staff_takeover_and_the_rate_limit(self):
        cv.set_mode(self.conn, WA, "human", now=self.clock)
        r = self.send("chest pain!!")
        self.assertTrue(r.emergency)
        self.assertEqual(len(r.replies), 1)
        self.assertFalse(r.silent)
        for _ in range(cv.RATE_LIMIT_TURNS + 2):
            self.send("hello", wa=WA2)
        r = self.send("heavy bleeding", wa=WA2)
        self.assertTrue(r.emergency)
        self.assertEqual(len(r.replies), 1)

    def test_emergency_is_not_sent_to_the_llm(self):
        self.send("can't breathe")
        self.assertEqual(self.picker.calls, [])


class ClinicalTests(ConvCase):
    def test_clinical_question_in_every_language(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                r = self.send(w["clinical"])
                self.assertReply(r, "clinical", lang)
                self.assertEqual(len(r.replies), 1)
                self.assertTrue(r.clinical)
                self.assertEqual(r.flag, "clinical")
                self.assertFalse(r.emergency)
                self.assertEqual(self.picker.calls, [])

    def test_clinical_text_is_the_specified_fixed_message(self):
        self.assertEqual(self.send("which tablet for my headache").replies[0].text,
                         "We can't give medical advice on chat. Please call the clinic or book a consultation.")

    def test_clinical_question_leaves_the_session_intact(self):
        self.patient()
        self.send("book")
        self.send("tomorrow")
        r = self.send("is this medicine ok with my diabetes")
        self.assertTrue(r.clinical)
        s = self.session()
        self.assertEqual((s["goal"], s["step"], s["slots"]["appt_date"]), ("book", "time", TOMORROW))
        r = self.send("4 pm")                                   # the booking carries on
        self.assertEqual(r.replies[0].buttons[0][0], "confirm:yes")

    def test_symptom_mentioned_in_a_booking_request_is_flagged_but_the_booking_continues(self):
        self.patient()
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn.execute("DELETE FROM wa_sessions")
                r = self.send(w["clinical_book"])
                self.assertTrue(r.clinical)
                self.assertReply(r, "clinical", lang)
                self.assertEqual(len(r.replies), 2)
                self.assertEqual(r.replies[1].text[:15], ct.text("offer_slots", lang, date=self.fd(TOMORROW, lang))[:15])
                self.assertEqual(self.session()["goal"], "book")

    def test_never_any_advice_text(self):
        for lang in ct.LANGUAGES:
            for key, by_lang in ct.MSG.items():
                text = by_lang[lang].lower()
                for word in ("take ", "dose of", "mg", "paracetamol"):
                    self.assertNotIn(word, text, (key, lang))


class EscalationTests(ConvCase):
    def test_talk_to_a_person_in_every_language(self):
        for lang, w in SAY.items():
            with self.subTest(lang):
                self.conn = make_db()
                r = self.send(w["human"])
                self.assertReply(r, "escalate", lang)
                self.assertEqual((r.escalate, r.flag), ("human_request", "escalation"))
                self.assertEqual(self.picker.calls, [])

    def test_human_request_mid_flow_drops_the_flow(self):
        self.patient()
        self.send("book tomorrow")
        r = self.send("please let me speak to someone")
        self.assertEqual(r.escalate, "human_request")
        self.assertIsNone(self.session()["goal"])

    def test_registration_requests_go_to_staff(self):
        r = self.send("I want to register")
        self.assertEqual(r.escalate, "register")
        self.assertEqual(r.replies[0].text, ct.text("escalate", "en"))


class TakeoverTests(ConvCase):
    def test_human_mode_keeps_the_agent_silent_and_flags_every_message(self):
        self.patient()
        cv.set_mode(self.conn, WA, "human", now=self.clock)
        for text in ("hello", "book tomorrow 4 pm", "blorp"):
            r = self.send(text)
            self.assertEqual(r.replies, [])
            self.assertTrue(r.silent)
            self.assertEqual(r.flag, "human_mode")
            self.assertIsNone(r.handoff)
        r = self.send(choice="menu:book")
        self.assertEqual(r.replies, [])
        self.assertEqual(self.picker.calls, [])

    def test_resume_agent_restarts_clean(self):
        cv.set_mode(self.conn, WA, "human", now=self.clock)
        cv.set_mode(self.conn, WA, "agent", now=self.clock)
        r = self.send("hello")
        self.assertEqual(r.replies[0].buttons[0][0], "menu:book")
        self.assertEqual(cv.get_mode(self.conn, WA), "agent")

    def test_take_over_drops_a_half_finished_flow(self):
        self.patient()
        self.send("book")
        cv.set_mode(self.conn, WA, "human", now=self.clock)
        s = self.session()
        self.assertEqual((s["goal"], s["step"], s["slots"]), (None, None, {}))

    def test_mode_validation_and_default(self):
        self.assertEqual(cv.get_mode(self.conn, "919000000000"), "agent")
        with self.assertRaises(ValueError):
            cv.set_mode(self.conn, WA, "robot")


class RateLimitTests(ConvCase):
    def test_31st_message_in_an_hour_goes_to_staff(self):
        for i in range(cv.RATE_LIMIT_TURNS):
            r = self.send("hello", minutes=1 if i else 0)
            self.assertIsNone(r.escalate, i)
        r = self.send("hello")
        self.assertEqual((r.escalate, r.flag), ("rate_limit", "escalation"))
        self.assertEqual(r.replies[0].text, ct.text("escalate", "en"))
        r = self.send("hello")                                  # still over the limit: silent, still to staff
        self.assertEqual((r.escalate, r.replies, r.silent), ("rate_limit", [], True))

    def test_the_limit_is_per_sender_and_resets_after_the_hour(self):
        for _ in range(cv.RATE_LIMIT_TURNS + 3):
            self.send("hello")
        self.assertIsNone(self.send("hello", wa=WA2).escalate)
        r = self.send("hello", minutes=61)
        self.assertIsNone(r.escalate)
        self.assertEqual(r.replies[0].buttons[0][0], "menu:book")


# ---------------------------------------------------------------------------
# The LLM stage
# ---------------------------------------------------------------------------

class IntentPickerTests(ConvCase):
    def test_only_called_when_rules_fail_and_nothing_is_pending(self):
        self.patient()
        self.send("hello")
        self.send("book tomorrow")                              # rules matched
        self.send("what is my token")
        self.send("9876")                                       # answer to the pending question
        self.assertEqual(self.picker.calls, [])
        self.conn.execute("DELETE FROM wa_sessions")
        self.send("zzz qqq")
        self.assertEqual(self.picker.calls, ["zzz qqq"])

    def test_not_called_with_a_pending_question(self):
        self.patient()
        self.send("book")
        self.send("zzz qqq")                                    # garbage at the day question
        self.assertEqual(self.picker.calls, [])

    def test_picked_label_starts_that_flow_but_never_supplies_slots(self):
        self.patient()
        self.picker.label = "book"
        r = self.send("could you fit me in at some point")
        self.assertEqual(self.session()["goal"], "book")
        self.assertEqual(self.session()["slots"], {})
        self.assertTrue(r.replies[0].buttons[0][0].startswith("day:"))

    def test_picked_labels_map_to_flows(self):
        pid = self.patient()
        self.appt(FRI, "10:00", patient_id=pid)
        for label, check in (("cancel", lambda r: r.replies[0].buttons[0][0] == "confirm:yes"),
                             ("reschedule", lambda r: r.replies[0].buttons[0][0].startswith("day:")),
                             ("status", lambda r: r.replies[0].kind == "status"),
                             ("greeting", lambda r: r.replies[0].buttons[0][0] == "menu:book"),
                             ("human", lambda r: r.escalate == "human_request")):
            with self.subTest(label):
                self.conn.execute("DELETE FROM wa_sessions")
                self.picker.label = label
                self.assertTrue(check(self.send("qwerty asdf")))

    def test_unclear_failing_or_invalid_picker_means_unclear(self):
        for picker in (FakePicker("unclear"), FakePicker(raises=RuntimeError("ollama down")),
                       FakePicker("delete_everything"), FakePicker(None)):
            with self.subTest(picker.label):
                self.conn.execute("DELETE FROM wa_sessions")
                self.picker = picker
                r = self.send("qwerty asdf")
                self.assertEqual(r.replies[0].text, ct.text("menu_unclear", "en"))
                self.assertIsNone(r.handoff)

    def test_llm_label_cannot_write_anything(self):
        self.picker.label = "book"
        before = self.counts()
        self.send("qwerty asdf")
        self.assertEqual(self.counts(), before)


# ---------------------------------------------------------------------------
# Language following
# ---------------------------------------------------------------------------

class LanguageTests(ConvCase):
    def test_replies_follow_the_most_recent_language(self):
        self.patient()
        r = self.send("hello")
        self.assertEqual(r.language, "en")
        r = self.send("mujhe appointment chahiye")
        self.assertEqual(r.language, "hinglish")
        self.assertEqual(r.replies[0].text[:15], ct.text("ask_day", "hinglish")[:15])
        r = self.send("नमस्ते")
        self.assertEqual(r.language, "hi")
        r = self.send("hello there my friend")                  # a longer English message switches back
        self.assertEqual(r.language, "en")

    def test_short_ascii_replies_do_not_flip_a_hindi_conversation_to_english(self):
        self.patient()
        self.send("मुझे अपॉइंटमेंट चाहिए")
        r = self.send("tomorrow")
        self.assertEqual(r.language, "hi")
        r = self.send("4 pm")
        self.assertEqual(r.language, "hi")
        self.assertIn("Sunita Devi", r.replies[0].text)   # the registered name, as stored

    def test_button_taps_keep_the_session_language(self):
        self.send("नमस्ते")
        r = self.send(choice="menu:book")
        self.assertEqual(r.language, "hi")


# ---------------------------------------------------------------------------
# Hard guarantees
# ---------------------------------------------------------------------------

class NeverWritesTests(ConvCase):
    def test_a_long_session_never_touches_appointments_proposals_or_the_audit_log(self):
        pid = self.patient()
        self.appt(FRI, "10:00", patient_id=pid)
        before = self.counts()
        script = ["hello", "book tomorrow 4 pm", ("confirm:yes",), "reschedule", "monday", "11 am", ("confirm:yes",),
                  "cancel my appointment", ("confirm:yes",), "what is my token", "blorp", "I have chest pain",
                  "which tablet for fever", "talk to a person"]
        for step in script:
            if isinstance(step, tuple):
                self.send(choice=step[0])
            else:
                self.send(step)
        self.assertEqual(self.counts(), before)

    def test_handle_inbound_writes_only_sessions_and_holds(self):
        tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        snapshot = lambda: {t: self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables}
        before = snapshot()
        self.patient()
        self.send("book tomorrow 4 pm")
        self.send(choice="confirm:yes")
        after = snapshot()
        changed = {t for t in tables if before[t] != after[t]}
        self.assertLessEqual(changed, {"patients", "wa_sessions", "slot_holds", "sqlite_sequence"})
        self.assertTrue({"wa_sessions", "slot_holds"} <= changed)

    def test_agent_enabled_switch(self):
        self.assertTrue(cv.agent_enabled({}))
        self.assertTrue(cv.agent_enabled({"WHATSAPP_AGENT_ENABLED": "1"}))
        self.assertFalse(cv.agent_enabled({"WHATSAPP_AGENT_ENABLED": "0"}))
        self.assertFalse(cv.agent_enabled({"WHATSAPP_AGENT_ENABLED": " 0 "}))
        self.assertTrue(cv.agent_enabled({"WHATSAPP_AGENT_ENABLED": "yes"}))

    def test_replies_are_always_template_text(self):
        """Every reply across a varied script is exactly one of the fixed
        templates (with values filled in) -- nothing else can appear."""
        self.patient()
        import re
        known = []
        for by_lang in ct.MSG.values():
            for text in by_lang.values():
                known.append(re.compile(re.sub(r"\\\{\w+\\\}", ".*", re.escape(text)), re.DOTALL))
        script = ["hello", "book", "blorp", "tomorrow", "4 pm", "11 pm", "cancel", "reschedule", "I have fever",
                  "chest pain", "talk to a person", "thanks"]
        for step in script:
            for reply in self.send(step).replies:
                if reply.kind != "message":
                    continue
                self.assertTrue(any(p.fullmatch(reply.text) for p in known), reply.text)


if __name__ == "__main__":
    unittest.main()
