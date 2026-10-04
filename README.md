# Clinic Copilot

A voice-driven admin assistant prototype for small Indian clinics. Staff **hold a key and speak** (English, Hindi or Hinglish) to look things up, book / cancel / reschedule appointments, log visits and expenses, run the day's patient queue, and more. Patients can also book over WhatsApp.

**Prototype status.** This is a working prototype, not a medical product. Use only fake data. It has no user login yet and has not had a security or compliance review.

## The safety idea

Speech and language models are never allowed to change records on their own:

1. Speech is turned into text, and **you can correct the text** before anything runs.
2. A small local model only *labels* the command (for example "cancel appointment"). Names, dates, times, amounts and phone numbers are read by plain rules, never invented by a model.
3. Every change becomes a **review card** that a human must **Approve**. There is no voice approval.
4. Approved changes are written in one transaction and recorded in an audit log.

## What it does

- **Hold to talk:** hold `Enter` or `F1`, speak, release. The mic is off whenever nothing is held.
- **Editable transcript:** the heard text appears in a box to fix before pressing Send.
- **Conversation memory:** "book him tomorrow at 5", "cancel the second one", "make it 6 pm instead"; the assistant asks for a missing patient, day or time. It forgets after 10 idle minutes.
- **Appointments with tokens,** a live queue (check in, call next, done, no-show), and a Google Calendar view (optional).
- **Patient WhatsApp agent** to book / reschedule / cancel / check status within guardrails (optional).
- **Automation tab** with booking blocks, a daily cap, and an undo feed for automated actions.
- **Names across scripts:** Devanagari speech matches Roman-letter names (अमित दुआ ↔ Amit Dua).

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
> "Hindi: अमित नाम के सारे अपॉइंटमेंट निकालो।"

### Speed on your machine

By default the model runs on the CPU, which was faster than the GPU on the developer's Mac. On yours it may differ:

```bash
PYTHONPATH=. .venv/bin/python scripts/bench_ollama.py
```

If the GPU is faster, add `CLINIC_LLM_NUM_GPU=auto` to `.env`.

## Settings

All settings live in `.env` (see `.env.example`). WhatsApp and Google Calendar are off unless you add their credentials; with none set, the app only *records* the WhatsApp messages it would have sent. Never commit `.env`, a database file, or a service-account key (they are in `.gitignore`).

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
| `clinic/conversation.py`, `clinic/whatsapp*.py` | the WhatsApp patient agent |
| `clinic/gcal_*.py` | one-way Google Calendar sync |
| `static/`, `templates/` | the browser UI (plain JavaScript, no build step) |
| `scripts/` | demo data, speed and accuracy checks |
| `tests/` | the automated tests |

## Known limits

- No login or roles yet; run it on a trusted machine or network only.
- Single clinic, single doctor, English / Hindi / Hinglish only.
- Real microphone use depends on your browser permissions and on Sarvam's service.
- Local-model accuracy is good but not perfect (about 96% on the project's 82 test phrases); the review step exists for that reason.
