# Clinic Copilot: notes for Claude

Voice and WhatsApp admin assistant for small Indian clinics. Flask + Flask-SocketIO + vanilla JS + SQLite,
a local model through Ollama, Sarvam for speech-to-text. Runs on `localhost:5050`. The README describes the
product; this file is what you need to work in the code safely.

## Rules that always apply

- **Never commit or push** (`git commit`, `git push`, `git add` for committing, tags, PRs) unless the user says so in
  that message. Approval for one commit does not carry over. Finish work as uncommitted edits and say so. Reading git
  state is fine. Never use `git stash`, `checkout`, `restore` or `reset` on the user's uncommitted work.
- **Never touch the real `clinic.db`.** Tests use temp DBs. For a backup or a scratch copy use
  `sqlite3 clinic.db ".backup '<dest>'"` and check `pragma integrity_check` and row counts. It runs in WAL mode, so a
  plain `cp` gives a stale copy. A scratch DB must not be named `clinic.db` (that also forces WhatsApp dry-run).
- **Restart the app by port**, not by name (`pkill` missed it once):
  `kill $(lsof -nP -iTCP:5050 -sTCP:LISTEN -t); sleep 2; (nohup .venv/bin/python app.py > /tmp/clinic_app.log 2>&1 < /dev/null &)`
  then confirm the new PID's start time is after your last edit. Python changes and `templates/` (cached) need a
  restart; `static/` files are served live, so keep every saved `.js`/`.css` valid (`node --check`).
- **Back up the database before restarting onto a schema change.** Schema changes are additive only.
- **No real WhatsApp, Sarvam, Meta or hosted-model calls from tests or scratch servers.** Live WhatsApp is the
  default when `WHATSAPP_NOTIFY_MODE` is unset; use `dry_run`. Never print, log or paste values from `.env`.
- **Sub-agents and worktrees:** an isolated worktree starts from the last commit, so it does NOT contain uncommitted
  work. When a task builds on uncommitted code, work in the main folder, or merge back with a three-way merge
  (`git merge-file`). A sub-agent must not touch `clinic.db` or the app on port 5050; give it its own scratch port
  and database.
- **Ask before running the full Python suite** (~1960 tests). Run only the tests for the area you changed (and the JS
  tests, `node tests/*.test.js`, after a JS change; no approval needed for these). Say which ones ran and that the full
  suite was not run, and offer it in one line; run it only after a yes. Never bundle it into a restart or any other
  command. When launching a sub-agent, tell it in its brief not to run the full suite unless the user approved it: it
  runs targeted tests and reports.
- Other people's overlapping edits are normal here. Edit surgically; never reformat or revert unrelated code.

## Run and test

```
.venv/bin/python app.py                                   # the app, port 5050
.venv/bin/python -m unittest discover -s tests -t .       # FULL suite ~1960 tests, ~1-2 min: ask first
.venv/bin/python -m unittest tests.test_unanswered        # one module: no approval needed
node tests/ptt_client.test.js                             # JS helpers; also tests/unanswered_ui.test.js
PYTHONPATH=. .venv/bin/python scripts/eval_planner.py     # LIVE model eval (--cases reads for the read set)
PYTHONPATH=. .venv/bin/python scripts/eval_intent.py      # LIVE routing eval; scripts/eval_names.py for names
```

Importing the `tests` package sets `INTENT_LLM_ENABLED=0`, so tests never call a live model; live scripts re-enable it.
Tests must not depend on today's real date (date time bombs have broken the suite twice): fix dates or inject a clock
(`notify.Now`). zsh: quote globs (`--include='*.py'`), write `${var}:path`, and `for p in $VAR` does not word-split.

## How a command is understood

**Staff (voice or typed)**: `realtime_voice` -> `voice_turns.handle_turn` -> `pipeline.transcript_to_response`.
1. Precise rules first, no model: switch branch, close a branch, "move ... appointment", patient count, a follow-up that
   needs what is on screen (`voice_context.contextual_intent`).
2. Keyword classifier `nlu/classify.py` always runs; the model outranks it when they disagree.
3. **Tool-calling planner** `nlu/planner.py`: one call to the local model returns ONE of ~19 typed tools
   (`nlu/tools.py`), validated, dates/times cross-checked by `nlu/date_guard.py` (the deterministic reader wins),
   then mapped to the `(intent, slots)` the rest of the app already understands. `clarify` asks one question;
   `unsupported` ends the command.
4. Fallbacks: one-word label picker (`nlu/intent_llm.py`) -> keyword rules -> "please rephrase".
5. Slot extraction -> entity resolution (names to ids in code) -> ask for what is missing -> read answer, or a proposal.

**Patients on WhatsApp** is a separate path with no planner: `conv_runtime.process_inbound` ->
`conversation.handle_inbound` (a dialogue state machine with buttons; choice ids like `branch:<id>`,
`closure:accept:<id>`, `followup:cancel:<id>`), keyword rules + a closed-list model picker. Auto policy commits within
limits (`auto_policy.py`), otherwise it goes to the staff inbox. Medical questions and confusion go to a human.

Switches: `INTENT_LLM_ENABLED`, `INTENT_PLANNER_ENABLED=0` (one-line rollback to the label picker),
`INTENT_PLANNER_SCOPE` (`all` default, or `unmatched`), `PLANNER_MODEL`, `PLANNER_TIMEOUT_S` (12), `CLINIC_LLM_MODEL`,
`CLINIC_LLM_NUM_GPU` (default 0 = CPU: Metal was ~6x slower on this Mac), `CLINIC_LLM_NUM_CTX`.
One local model (`gemma4:12b`) serves everything. A call times out after 12 s and falls back; the page warms the model.

## Design invariants (do not weaken)

- **The model never emits an id and never writes.** Names and branch letters are spoken text; code resolves them.
- **Every write is a proposal behind a review card and a human Approve** (`core.propose` / `core.confirm`), recorded
  in the immutable `audit_log`. **No voice approval, ever.** Memory only pre-fills or edits cards and answers reads.
- **Reads go through the whitelist in `clinic/query_tool.py`**: fixed tables and columns, bound parameters,
  `PRAGMA query_only`, a 200-row cap, no model-written SQL. Diagnoses, visit and appointment notes, message bodies,
  tokens and WhatsApp ids are never exposed (tests plant a secret in each excluded column). A read nothing can answer
  is saved to `unanswered_questions` for a person to add; **the app never creates whitelist entries by itself.**
- Closure planning (`closures.plan`) writes nothing; Apply writes, under `auto_actions.COMMIT_LOCK`, with Undo.
- Staff voice memory is in server memory per browser connection, 10-minute idle timeout, cleared on reload.

## Domain facts

- **Branches:** `branch_id=None` means the default branch. A branch is open when a doctor is scheduled. Tokens are per
  branch (A-T01). "My branch" and the Viewing switcher live in `localStorage` (`clinic.myBranch`, `clinic.viewBranch`).
- **Follow-ups:** creating one books a real appointment slot immediately (`clinic/followups.py`). Two WhatsApp reminders:
  N days before at a set time (default 2 days, 10:00) and N hours before the slot (default 4, never earlier than 07:00),
  clinic-wide settings in `app_settings`. A reminder missed while the app was down is sent on the next tick. No
  diagnosis in any patient message; the internal `followups.diagnosis` note is never sent or read out.
- **WhatsApp 24-hour rule:** free-form text only within 24 h of the patient's last message; otherwise only a Meta-approved
  template. Template definitions are in `docs/meta_templates/`; sending is gated by the "approved templates" list
  ticked in Settings, so an unapproved template is never sent and the reminder waits in "Send manually".
- **Auto-registration:** booking for an unknown name plus a 10-digit phone registers the patient (`intents.py`).
- Dates are ISO; times 24 h; money is stored in paise; the clinic runs on IST (the machine clock is treated as IST).

## Where things live

`app.py` routes and wiring | `clinic/schema.sql` + `clinic/db.py` (additive upgrades) | `clinic/intents.py` write handlers |
`clinic/notify.py` outbox, templates, 24 h window | `clinic/scheduler.py` 60 s tick | `clinic/closures.py`,
`voice_closure.py` | `clinic/followups.py`, `followup_notify.py` | `clinic/planner_log.py`, `unanswered.py` |
`static/live_voice.js` (assistant UI), `review_card.js`, `nav.js` (tabs), `branches.js`, `save_tick.js` |
`templates/dashboard.html` | `tests/` | `docs/meta_templates/`.

## UI conventions

Build DOM with `textContent` (never `innerHTML` with data). Use the design tokens in `static/style.css`
(`--accent`, `--ok-*`, `--danger-*`). A successful Save shows only a check mark beside the button via `SaveTick.show(button)`;
failures keep a text message. The Patients tab has sub-tabs (Patients, Missed follow-ups, Attendance today,
Follow-ups); the "Patient messages" tab is hidden for now.

## Known limits

- The Mac has 16 GB and swaps heavily, so local-model calls can take seconds to minutes; the first call after idle is slowest.
- Latest measured planner accuracy: about 91% (tool and arguments) on the original 65 commands, 66% on 62 new read
  questions (before later fixes); no unsafe write in any refusal case. Hindi/Hinglish phrasing is the weakest area.
- Meta templates must be approved by Meta before out-of-window reminders send; the Hindi and Hinglish ones may be
  rejected or reclassified.
- `scripts/planner_bench/` (model benchmark, may contain a Claude baseline runner) is untracked on purpose.
