# Clinic Copilot

A voice-driven admin assistant prototype for small Indian clinics. Staff **hold a key and speak** (English, Hindi or Hinglish) to look things up, book / cancel / reschedule appointments, log visits and expenses, run the day's patient queue, and more. Patients can also book over WhatsApp. It works for one clinic or for **several branches of one brand**.

**Prototype status.** This is a working prototype, not a medical product. Use only fake data. It has no user login yet and has not had a security or compliance review.

## The safety idea

Speech and language models are never allowed to change records on their own:

1. Speech is turned into text, and **you can correct the text** before anything runs.
2. A local model only *plans* the command (it picks one typed tool, for example "cancel appointment", and fills in what was said). It never produces an id, a date or time it cannot be checked on (plain rules re-read those and win), and a bad answer is thrown away. Names are matched to real records by plain code.
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
- **Follow-ups with reminders:** Patients > Follow-ups books a return visit into the calendar and sends the patient two WhatsApp reminders. See [Follow-up reminders](#follow-up-reminders).
- **Automation tab** with booking blocks, closures, a daily cap, and an undo feed for automated actions.
- **Reads by voice:** appointments for a day, week or person; free slots; the queue; a patient's phone number; **how many patients are registered**.
- **New patients are registered by the booking itself** when staff give a name and a 10-digit phone (the same person on the same phone is reused, not duplicated).
- **Names across scripts:** Devanagari speech matches Roman-letter names (अमित दुआ ↔ Amit Dua).
- Google Calendar sync still exists but is **off by default** (`GOOGLE_CALENDAR_SYNC=1` turns it on); the in-app calendar is the main view.

## What you need

- A Mac or Linux machine with **Python 3.9 or newer**. (Developed and tested on macOS, Apple silicon.)
- **[Ollama](https://ollama.com)** with the one local model the app uses (about 8 GB, so a 16 GB Mac is the minimum):
  ```bash
  ollama pull gemma4:12b
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

The first command after the model has been idle is slow (it has to be loaded back into memory, and on a Mac that is short of memory that can take minutes). Opening the Assistant page warms it in the background; while it thinks the page shows "Working out what you meant...".

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
> "Branch B will be closed for next one week, move all appointments to Branch C." → a batch card that tries Branch C first for everyone
> "List the patients." then "give me the names as well." / "Which patients are over 60?" / "Show Amit's appointments."

### Speed on your machine

By default the model runs on the CPU, which was faster than the GPU on the developer's Mac. On yours it may differ:

```bash
PYTHONPATH=. .venv/bin/python scripts/bench_ollama.py
```

If the GPU is faster, add `CLINIC_LLM_NUM_GPU=auto` to `.env`.

## The tool-calling planner

Where the app used to ask the local model for one word (the name of the command), it now asks a **planner**: one call to the local model (Gemma 4 12B, through Ollama's native tool calling) that returns *one typed tool call* out of 19 (`clinic/nlu/tools.py`: book / reschedule / cancel an appointment, close a branch, doctor leave, register a patient or staff member, record a visit, set / cancel / reschedule a follow-up, log an expense or attendance, queue actions, a read-only `query`, switch branch, open the calendar, `clarify` and `unsupported`). The tool call is turned into exactly what the old path produced, so everything after it is unchanged: entity resolution, the review card, answers and the closure plan.

- **Order.** The precise rules still come first and never wait for the model: a branch switch, closing a branch, an explicit move, a patient count, and a follow-up on what is on screen ("cancel the second one"). The keyword rules stay as the backup. Then the planner is asked where the one-word picker used to be.
- **Safe by construction.** The model never emits an id (names are spoken text, branches are letters). Unknown tools, unknown arguments, wrong types, a missing required argument or a date that is not a date throw the whole call away. Every write still ends in a review card; closing a branch or a doctor's leave only produces a plan.
- **Dates and times.** After the model answers, its dates and times are re-read from your words by the existing date extractor, and the extractor wins when it can read the phrase ("next Monday", "kal", "12th October", Hindi forms). "Closed for the next one week" counts seven days from the first day said, or from today when none was said (end = start + N - 1). A phrase the extractor cannot read ("parso") keeps the model's value.
- **When it cannot help** (more than `PLANNER_TIMEOUT_S`, 12 s by default; Ollama down; an invalid call) the old one-word picker runs, then the keyword rules, then the usual "could not classify, please rephrase" message. Nothing is ever written on a fallback without the review card.
- **Asking back.** If a required detail is missing, the planner's `clarify` becomes the assistant's question; your next sentence is planned again with the question and your first words as the previous turn.
- **Follow-ups.** "Give me the names as well" or "and the day after?" work because the previous turn (what you said, the call it became, the answer) is kept in the same ten-minute conversation memory as before.
- **Closing a branch.** "Branch B will be closed for next one week, move all appointments to Branch C" sets the first day, last day and a *preferred destination*: Branch C is tried first for every patient (it needs a doctor and a free time within two hours), and the row says so when it had to fall back to the nearest branch.
- **Generic reads** (`clinic/query_tool.py`). Questions the dedicated reads cannot answer become a structured spec over a fixed whitelist of tables, columns and filters. The model never writes SQL; every value is a bound parameter; the query runs read-only (`PRAGMA query_only`) with at most 200 rows. See "What the Assistant can read" below.

Measured on the 65-command planner set (`scripts/eval_planner.py`, Gemma 4 12B on CPU): right tool 92%, right tool and arguments 91% after the date / time safety net (78% before it), and no command that should have been a question or a refusal became a write. Speed depends on free memory: about 7 s median on that run, with slow spikes (minutes) when the Mac was swapping. On the 62 newer read questions (`--cases reads`: new record types, sums, sort, month ranges, a few unanswerable ones) the same model chose the right tool 81% of the time and tool plus arguments 66%; its usual slips are leaving out a branch or `limit`, omitting a `measure` (now defaulted in code where only one number makes sense), and sending an unanswerable question to a near-miss record type. The 82-command routing replay against the old one-word picker has not been run yet, so check `scripts/eval_planner.py --replay` before relying on the planner for everything.

Switches (all in `.env`; see `.env.example`). **To roll back, set `INTENT_PLANNER_ENABLED=0` and restart.**

| Setting | Effect |
|---|---|
| `INTENT_PLANNER_ENABLED=0` | roll back to the one-word picker with no other change |
| `INTENT_PLANNER_SCOPE=unmatched` | ask the planner only when the keyword rules find nothing (faster, but the rules' own mistakes then stand) |
| `PLANNER_TIMEOUT_S` | how long to wait before falling back (12) |
| `CLINIC_LLM_MODEL`, `PLANNER_MODEL` | the one local model (`gemma4:12b`); the planner follows it unless `PLANNER_MODEL` says otherwise |

**The planner log.** Every command that reaches the planner is recorded in the local `planner_log` table (what was heard, the previous turn, the tool call, the route taken, the time it took, any date/time override, and later whether the card was approved or rejected). It is on by default and is never sent anywhere; turn it off with the app setting `planner_log_enabled` = `0` (`clinic/settings.py`). **The transcripts contain patient names, so the table, and anything exported from it, must stay on this machine** (do not commit or share it). `scripts/export_planner_log.py --db clinic.db > new_cases.py` turns the log into draft test cases (`--problems` for the commands that fell back or were rejected) for a person to review before adding them to `tests/planner_eval_cases.py`.

## What the Assistant can read

The planner's `query` tool covers 17 record types: patients, appointments, free slots, follow-ups (with time, doctor, branch and whether a slot is booked), the cash book, **staff, attendance, branches** (address, open or closed on a day and why), **doctors, doctor schedules** ("who is on duty now" uses the clock), **visits** (fees in rupees), **expenses**, **reminders** (the WhatsApp messages the app sent, with a plain-words kind and status and a short, scrubbed error), **closures** (with how many patients were moved), **booking blocks**, the **audit log** (readable action plus a summary built from a fixed list of safe keys) and **patient activity**. Besides listing and counting it can **sum, average, min and max** a number (fees, expenses, appointment length, patient age, patients moved), **group** a count or sum (by doctor, branch, status, day, month, weekday, expense description ...), and **sort** (newest, oldest, highest, lowest, name, with a limit): "how much did we collect this month", "appointments per doctor", "the biggest expense", "the last 5 patients". The answer sentence is fixed wording written by code ("Total fees collected this month: Rs 12,400"), never by the model. "This month" and "last month" ("is mahine", "pichle mahine") are read by code as a whole calendar month and win over the model's dates.

**Never readable, however a question is phrased:** visit notes, diagnoses (including a follow-up's internal diagnosis), appointment notes, WhatsApp message text, phone numbers and WhatsApp ids in logs, tokens and keys, who a payment was made to, the raw audit payload. Those columns are simply not in the whitelist (and `tests/test_query_entities.py` plants a secret in each and proves none can come out). A branch, staff or doctor question is clinic-wide unless a branch is named; appointments still default to this computer's My branch.

## Questions the Assistant couldn't answer

When a command is clearly a request for information the app cannot read yet, the Assistant says so, saves the question, and tells you when it works. Nothing is ever generated automatically: **the app does not write, draft or enable whitelist entries or SQL by itself.**

1. **Asked.** The planner called `unsupported` with a `wanted` ("profit this month"), or called `query` with something outside the whitelist (an unknown record type, column, filter, measure or group-by), or the command ended as "please rephrase" and reads like a question (question words or "show / list / how many / kitne / kab / kaun" plus a clinic noun; small talk and commands that change something are not counted).
2. **Captured.** The question goes into the local `unanswered_questions` table (deduplicated on its lowercase, punctuation-free text; `times_asked` counts repeats) and the Assistant replies with a fixed sentence in English, Hindi or Hinglish ("I can't answer that yet. I will work on it."; asked again: "...asked 3 times..."). Saving obeys the `planner_log_enabled` setting; when it is off the reply simply does not promise to save. **Transcripts can contain patient names, so the table stays on this machine; do not commit or share it.**
3. **Reviewed.** Audit log tab, "Questions I couldn't answer": the question, how many times, when last, its status, with *Mark as added*, *Mark as building*, *Dismiss* and *Reopen*. `scripts/export_unanswered.py --db clinic.db` prints the open ones, most asked first, with what the planner said they wanted and the query spec it tried (a hint, never trusted or run), ready to paste into a build task.
4. **Added by a developer.** Add the entity / field / filter to `clinic/query_tool.py` (and one short line to the `query` tool in `clinic/nlu/tools.py`), with tests. Keep clinical text out of it.
5. **Marked resolved.** *Mark as added* asks for one line: what they can now ask ("Ask: who is on duty now").
6. **The user is told, once.** The next time the Assistant tab opens (or after the next turn if it stays open) a small notice reads "You asked '...' earlier. It works now: <your line>. Try it again." and is never shown again for that question. A question asked again after being resolved or dismissed is reopened.

Routes: `GET /unanswered/data`, `POST /unanswered/<id>/status`, `GET /unanswered/notices`, `POST /unanswered/<id>/notified`. (A later phase could let an admin approve a declarative, planner-drafted entry from a catalog of allowed tables and columns, still never SQL; that is not built.)

## Several branches

- The first time the app starts on a database with one branch, it creates **two example branches (B and C)** and two example doctors so you can try it. Edit or remove them in **Settings → Branches**. A fresh clone running only the demo script has just Branch A until `app.py` has started once.
- A **branch is open when a doctor is scheduled there** (Settings → Doctor schedules, per weekday). One doctor per branch at a time; a doctor can't be at two branches at once. Slots, free times and bookings all follow those hours.
- **My branch** is chosen once per computer (Settings → This computer) and is where new bookings, lists and the queue default to. The **Viewing** switcher in the sidebar changes what the screens show: one branch or all.
- Patients on WhatsApp are asked which branch (nearest first when they type their PIN code; their last branch first otherwise). Reminders and confirmations carry the branch name, address, map link and doctor.
- By voice, name the branch ("at Branch B", "ब्रांच बी में", "B mein") or leave it out to use My branch. "All branches" works for reads.
- Branch locations are PIN codes only (no map coordinates yet), so "nearest" is by PIN-code closeness.

## Follow-up reminders

A **follow-up** is a return visit the doctor advised. Create them in **Patients > Follow-ups**: add a row per patient (patient, date, time, doctor, branch, and an optional diagnosis note), press **Review** to see which rows are fine, then **Book**. Each row books a real calendar slot straight away (the same checks as any booking: doctor hours, double booking, booking blocks, tokens), so nobody books it a second time. A day that is a closure, a doctor's leave, a booking block or a day off is refused and the next open day is offered. One bad row never stops the others, and **Undo this batch** takes the whole batch back.

- **The diagnosis note is for the clinic only.** It is never put in a WhatsApp message, a template, or the appointment.
- **Two reminders** go to the patient on WhatsApp: **2 days before** the follow-up date at **10:00**, and **4 hours before** the visit but never earlier than **07:00** that day. Both carry three buttons: **Reschedule** (the normal slot-picking chat), **Already visited** (the follow-up is closed and the slot is freed) and **Cancel** (asks "are you sure?" first). Change the timing in **Settings > Follow-up reminders**.
- **Catch-up:** every scheduler tick sends any reminder whose time has passed while the visit is still ahead, so a follow-up booked late, or an app that was switched off, still reminds. If both were missed, only the 4-hour one is sent. Nothing is sent once the visit has started.
- A patient who replies **STOP** gets no more reminders (**START** brings them back).
- Moving, cancelling or completing the appointment (from the Queue tab, by the patient on WhatsApp, or in a closure) updates the follow-up and its reminders.
- **Outside WhatsApp's 24-hour window** a reminder can only go as a Meta-approved template. The six texts to submit are in [`docs/meta_templates/`](docs/meta_templates/README.md); tick each one in **Settings** once Meta approves it. Until then, such reminders wait in **Send manually** (with the exact text to copy and a "Mark as sent manually" button).
- A follow-up made by the older voice command ("follow up in 7 days") is unchanged: a date only, no slot, no reminders.
- A follow-up patient can get up to four messages (the 2-day reminder, the day-before and morning appointment reminders, and the 4-hour reminder); the two sets are independent.

## Settings

All settings live in `.env` (see `.env.example`). WhatsApp and Google Calendar are off unless you add their credentials; with none set, the app only *records* the WhatsApp messages it would have sent. Never commit `.env`, a database file, or a service-account key (they are in `.gitignore`).

**WhatsApp's 24-hour rule.** WhatsApp only allows free-form messages to someone who messaged the clinic in the last 24 hours. Anything else (a reminder, or a closure notice to a patient who hasn't written recently) waits as "blocked, outside the 24-hour window" until a Meta-approved message template is set up. Only the follow-up reminders can be sent as templates today (see [Follow-up reminders](#follow-up-reminders)); the other templates' texts are prepared in `clinic/notify.py`, but sending them is not built.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .      # Python (no network, no live model)
node tests/ptt_client.test.js                            # browser logic (needs Node)
node tests/unanswered_ui.test.js                         # the unanswered-questions card, notice and table formatting

PYTHONPATH=. .venv/bin/python scripts/eval_intent.py     # live check of the one-word command picker (needs Ollama)
PYTHONPATH=. .venv/bin/python scripts/eval_names.py      # live check of name extraction
PYTHONPATH=. .venv/bin/python scripts/eval_planner.py    # live check of the planner on 65 commands (slow)
PYTHONPATH=. .venv/bin/python scripts/eval_planner.py --cases reads   # only the 62 newer read questions (records, sums, sort, months, unanswerable)
PYTHONPATH=. .venv/bin/python scripts/eval_planner.py --replay   # the 82 routing commands: planner path vs the one-word picker
```

## Where things are

| Path | What it holds |
|---|---|
| `app.py` | Flask app, routes, Socket.IO wiring |
| `clinic/nlu/` | command labelling (rules + local model), dates, times, names |
| `clinic/nlu/tools.py`, `planner.py`, `date_guard.py` | the planner: 19 typed tools and their validation, the model call and fallbacks, the date / time safety net |
| `clinic/query_tool.py`, `clinic/planner_log.py` | the whitelisted read-only query (17 record types, sums, group-by, sort), and the local planner log |
| `clinic/unanswered.py`, `scripts/export_unanswered.py` | questions the Assistant could not answer: capture, fixed replies, status, the one-time notice, the developer queue |
| `clinic/pipeline.py`, `clinic/voice_turns.py`, `clinic/voice_context.py` | turning a transcript into a read, a card or a question, and the conversation memory |
| `clinic/realtime_voice.py` | hold-to-talk speech session |
| `clinic/core.py`, `clinic/intents.py` | propose → approve → write, with the audit log |
| `clinic/conversation.py`, `clinic/whatsapp*.py` | the WhatsApp patient agent (including the branch question) |
| `clinic/branches.py`, `clinic/scheduling.py`, `clinic/token_queue.py` | branches, doctors and their schedules, free slots, per-branch queues and tokens |
| `clinic/voice_branch.py`, `clinic/voice_closure.py` | naming a branch, switching My branch, and closing a branch by voice |
| `clinic/closures.py`, `clinic/closure_notify.py`, `clinic/booking_blocks.py` | closing a branch: plan, apply, undo, the patient notice, and plain booking blocks |
| `clinic/followups.py`, `clinic/followup_notify.py` | follow-ups with a booked slot: plan / apply / undo, the two reminders and their catch-up, syncing with the appointment, the patient's button replies, and the fixed message and Meta template texts |
| `docs/meta_templates/` | the WhatsApp templates to submit to Meta (README and JSON) |
| `clinic/auto_policy.py`, `clinic/auto_actions.py` | the guard checks and audited commit for automatic WhatsApp bookings, and Undo |
| `clinic/gcal_*.py` | one-way Google Calendar sync (off by default) |
| `static/`, `templates/` | the browser UI (plain JavaScript, no build step) |
| `scripts/` | demo data, speed and accuracy checks |
| `tests/` | the automated tests |

## Known limits

- No login or roles yet; run it on a trusted machine or network only.
- One brand with several branches and one doctor per branch at a time; English / Hindi / Hinglish only.
- The voice assistant understands a fixed list of tools (a local model plans one, rules and code check it). Adding a new kind of command means adding a tool, a card and tests. The old keyword rules are all still in place as the backup.
- The planner is a 12 GB-class model: slow on a Mac that is short of memory (a call can take far longer than the 12 s limit, and then the one-word picker answers instead), and the first command after idle time is the slowest.
- A reschedule over WhatsApp stays at the appointment's own branch, except after a closure notice ("Choose another"), which lets the patient pick a branch.
- Closure notices and the ordinary appointment reminders can't reach patients outside WhatsApp's 24-hour window yet; only the follow-up reminders can, once their templates are approved (see Settings).
- A "STOP" opt-out applies to the phone number, so people sharing one number share it. Staff cannot yet switch it back on from the dashboard (the patient sends START).
- Real microphone use depends on your browser permissions and on Sarvam's service.
- Local-model accuracy is good but not perfect (see `scripts/eval_planner.py` for the current numbers on the project's 65 planner and 82 routing phrases); the review step exists for that reason.
