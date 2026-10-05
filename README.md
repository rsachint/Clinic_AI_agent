# Clinic Copilot

A voice-driven admin assistant prototype for small Indian clinics. Staff **hold a key and speak** (English, Hindi or Hinglish) to look things up, book / cancel / reschedule appointments, log visits and expenses, run the day's patient queue, and more. Patients can also book over WhatsApp. It works for one clinic or for **several branches of one brand**.

**Prototype status.** This is a working prototype, not a medical product. Use only fake data. It has no user login yet and has not had a security or compliance review.

## The safety idea

Speech and language models are never allowed to change records on their own:

1. Speech is turned into text, and **you can correct the text** before anything runs.
2. A small local model only *labels* the command (for example "cancel appointment"). Names, dates, times, amounts and phone numbers are read by plain rules, never invented by a model.
3. Every change becomes a **review card** that a human must **Approve**. There is no voice approval.
4. Approved changes are written in one transaction and recorded in an audit log.
5. The one exception is a patient's own booking over WhatsApp, which the app may complete by itself when every schedule check passes (it writes the appointment and nothing else, and can be undone). Switch it off in the Automation tab.

## What it does

- **Hold to talk:** hold `Enter` or `F1`, speak, release. The mic is off whenever nothing is held.
- **Editable transcript:** the heard text appears in a box to fix before pressing Send.
- **Conversation memory:** "book him tomorrow at 5", "cancel the second one", "make it 6 pm instead"; the assistant asks for a missing patient, day or time. It forgets after 10 idle minutes.
- **Appointments with tokens** and a live queue (check in, call next, done, no-show). Each branch has its own queue and its own token numbers (A-T01, B-T01 ...).
- **In-app calendar** (Appointments tab): week, month and agenda views, one colour per branch, details on hover, click to open in the Queue.
- **Several branches:** each branch has its own doctor schedule, queue and bookings. See [Several branches](#several-branches).
- **Closing a branch:** one reviewed batch stops new bookings, moves or cancels everyone already booked, tells each patient on WhatsApp (Accept / Choose another) and can be undone.
- **Patient WhatsApp agent** to book / reschedule / cancel / check status within guardrails (optional). With several branches it asks which one, nearest first by PIN code.
- **Automation tab** with booking blocks, closures, a daily cap, and an undo feed for automated actions.
- **Reads by voice:** appointments for a day, week or person; free slots; the queue; a patient's phone number; **how many patients are registered**.
- **New patients are registered by the booking itself** when staff give a name and a 10-digit phone (the same person on the same phone is reused, not duplicated).
- **Names across scripts:** Devanagari speech matches Roman-letter names (अमित दुआ ↔ Amit Dua).
- Google Calendar sync still exists but is **off by default** (`GOOGLE_CALENDAR_SYNC=1` turns it on); the in-app calendar is the main view.

## What you need

- A Mac or Linux machine with **Python 3.9 or newer**. (Developed and tested on macOS, Apple silicon.)
- **[Ollama](https://ollama.com)** with the small model the app uses:
  ```bash
  ollama pull qwen2.5:3b-instruct
  ```
- A **[Sarvam](https://www.sarvam.ai) API key** for speech-to-text. Optional: without it everything works except the microphone.
- A modern browser (Chrome is the safest). The microphone works on `http://localhost` and on HTTPS addresses.

## Run it

```bash
# 1. get the code and install
git clone <your-repo-url> clinic-copilot
cd clinic-copilot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. configure (everything is optional except a Sarvam key for voice)
cp .env.example .env
#    edit .env and set SARVAM_API_KEY=...

# 3. create a database full of FAKE data
PYTHONPATH=. .venv/bin/python scripts/seed_demo.py

# 4. start the app
.venv/bin/python app.py
```

Open <http://localhost:5050>, click the **Assistant** tab, then hold `Enter` (or `F1`) and speak.

Make sure Ollama is running (`ollama serve`, or the Ollama menu-bar app) so the language model is available. If it isn't, the app falls back to simple keyword rules; it can still read registered patients' names, but a *new* name (for example when registering a patient) is left blank for you to fill in.

### Things to try

> "Show appointments for tomorrow."
> "Who is with the doctor right now?" / "Call the next patient."
> "Book an appointment for Priya Shah tomorrow at 5 pm." → a review card appears → **Approve**
> "Cancel Amit Anand's appointment."
> "Show all appointments of Amit." (finds both Amits, any date)
> "How many patients are registered?"
> "Book Priya Shah at Branch B tomorrow at 11." / "Move Amit's appointment to Branch C at 3." / "Show the queue at Branch B."
> "Switch to Branch C." (changes which branch this computer works for)
> "Close Branch A tomorrow because the doctor is ill." → a batch card with a proposed new slot for every patient → review → **Apply**
> "Hindi: अमित नाम के सारे अपॉइंटमेंट निकालो।"

### Speed on your machine

By default the model runs on the CPU, which was faster than the GPU on the developer's Mac. On yours it may differ:

```bash
PYTHONPATH=. .venv/bin/python scripts/bench_ollama.py
```

If the GPU is faster, add `CLINIC_LLM_NUM_GPU=auto` to `.env`.

## Several branches

- The first time the app starts on a database with one branch, it creates **two example branches (B and C)** and two example doctors so you can try it. Edit or remove them in **Settings → Branches**. A fresh clone running only the demo script has just Branch A until `app.py` has started once.
- A **branch is open when a doctor is scheduled there** (Settings → Doctor schedules, per weekday). One doctor per branch at a time; a doctor can't be at two branches at once. Slots, free times and bookings all follow those hours.
- **My branch** is chosen once per computer (Settings → This computer) and is where new bookings, lists and the queue default to. The **Viewing** switcher in the sidebar changes what the screens show: one branch or all.
- Patients on WhatsApp are asked which branch (nearest first when they type their PIN code; their last branch first otherwise). Reminders and confirmations carry the branch name, address, map link and doctor.
- By voice, name the branch ("at Branch B", "ब्रांच बी में", "B mein") or leave it out to use My branch. "All branches" works for reads.
- Branch locations are PIN codes only (no map coordinates yet), so "nearest" is by PIN-code closeness.

## Settings

All settings live in `.env` (see `.env.example`). WhatsApp and Google Calendar are off unless you add their credentials; with none set, the app only *records* the WhatsApp messages it would have sent. Never commit `.env`, a database file, or a service-account key (they are in `.gitignore`).

**WhatsApp's 24-hour rule.** WhatsApp only allows free-form messages to someone who messaged the clinic in the last 24 hours. Anything else (a reminder, or a closure notice to a patient who hasn't written recently) waits as "blocked, outside the 24-hour window" in the Patient messages tab until Meta-approved message templates are set up. The template texts are prepared in `clinic/notify.py`, but sending them is not built.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .      # Python (no network, no live model)
node tests/ptt_client.test.js                            # browser logic (needs Node)

PYTHONPATH=. .venv/bin/python scripts/eval_intent.py     # live check of command routing (needs Ollama)
PYTHONPATH=. .venv/bin/python scripts/eval_names.py      # live check of name extraction
```

## Where things are

| Path | What it holds |
|---|---|
| `app.py` | Flask app, routes, Socket.IO wiring |
| `clinic/nlu/` | command labelling (rules + local model), dates, times, names |
| `clinic/pipeline.py`, `clinic/voice_turns.py`, `clinic/voice_context.py` | turning a transcript into a read, a card or a question, and the conversation memory |
| `clinic/realtime_voice.py` | hold-to-talk speech session |
| `clinic/core.py`, `clinic/intents.py` | propose → approve → write, with the audit log |
| `clinic/conversation.py`, `clinic/whatsapp*.py` | the WhatsApp patient agent (including the branch question) |
| `clinic/branches.py`, `clinic/scheduling.py`, `clinic/token_queue.py` | branches, doctors and their schedules, free slots, per-branch queues and tokens |
| `clinic/voice_branch.py`, `clinic/voice_closure.py` | naming a branch, switching My branch, and closing a branch by voice |
| `clinic/closures.py`, `clinic/closure_notify.py`, `clinic/booking_blocks.py` | closing a branch: plan, apply, undo, the patient notice, and plain booking blocks |
| `clinic/auto_policy.py`, `clinic/auto_actions.py` | the guard checks and audited commit for automatic WhatsApp bookings, and Undo |
| `clinic/gcal_*.py` | one-way Google Calendar sync (off by default) |
| `static/`, `templates/` | the browser UI (plain JavaScript, no build step) |
| `scripts/` | demo data, speed and accuracy checks |
| `tests/` | the automated tests |

## Known limits

- No login or roles yet; run it on a trusted machine or network only.
- One brand with several branches and one doctor per branch at a time; English / Hindi / Hinglish only.
- The voice assistant understands a fixed list of commands (rules plus a small local model that only picks the command). Adding a new kind of command means adding rules, a card and tests; a more flexible design is possible but not built.
- A reschedule over WhatsApp stays at the appointment's own branch, except after a closure notice ("Choose another"), which lets the patient pick a branch.
- Closure notices and reminders can't reach patients outside WhatsApp's 24-hour window yet (see Settings).
- Real microphone use depends on your browser permissions and on Sarvam's service.
- Local-model accuracy is good but not perfect (about 98% on the project's 82 test phrases); the review step exists for that reason.
