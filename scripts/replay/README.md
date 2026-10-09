# Replay harness

Scripted multi-turn conversations driven through the real voice assistant (`voice_turns.handle_turn`), scored turn by
turn, written to an Excel report that shows how each conversation was evaluated. Conversations live in
`tests/replay_cases/` (plain data; all names and numbers are made up; clock fixed at Fri 2026-10-09 10:00 IST).

    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --list
    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config classic_rules_only classic_scripted model_first_scripted

Output: `replay_results/replay_<UTC timestamp>.xlsx` plus a sibling `.json` (git-ignored: it holds transcripts) and a
one-screen summary on stdout. Filters: `--cases manju nalin` (id contains), `--category regression dialogue`.

## Configs

| config | what it is | cost |
|---|---|---|
| `classic_rules_only` | classic mode, planner off: keyword rules and deterministic readers only (the name reader knows registered names only) | free, offline |
| `classic_scripted` | classic mode; the planner's answer is the turn's golden `model` | free, offline |
| `model_first_scripted` | model-first mode, same golden answers | free, offline |
| `classic_live`, `model_first_live` | the real Sarvam backend | about Rs 0.11 per call |

**The scripted configs do not measure the model.** The "model" is the script, so they test the app: the state card, the
dispatcher, the validators, memory carry-over and the fallbacks. Only the live configs measure real accuracy and latency.
Failures in `classic_rules_only` are expected where rules cannot do the job; that is what it shows.

Live runs need `--live` AND `--yes` AND `SARVAM_API_KEY` (environment, or the app's `.env`); the estimate of calls and
rupees is printed first, calls are made one at a time under about 3 per second, the planner's 5 s budget and circuit
breaker apply, and the key is scrubbed from all output. Nothing in the test suite can reach them.

    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config classic_live model_first_live --live --yes
    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config model_first_live --live --yes --ablate pending,card,remembered,list,last_turns,task

`--ablate` (live only) re-runs `model_first_live` once per listed state-card section dropped; the Ablation sheet lists
which turns stop passing without each piece of memory. The scripted configs ignore it.

## Reading the report

Sheets: Read me, Summary, Conversations (with a link to each conversation's first row in Turns), Turns (one row per turn:
what was said, the state card sent, planner tool + args, route, app result, expected, one PASS/FAIL column per check,
verdict, why it failed, latency / tokens / cost), Failures, Comparison (configs side by side), Ablation.

Verdicts: PASS, FAIL, KNOWN GAP (a conversation marked `known_gap="reason"`: behaviour not built yet), SKIPPED (the
conversation is only meaningful under other configs).

Checks (a turn's `expect`; all optional): `kind` (ask | card | read | note | error | navigate | card_update | switch_branch
| closure; a list means any of them), `intent`, `ask_kind`, `slots` (subset; `branch` is the letter), `options_count`,
`options_contain`, `rows`, `patient` (key from the setup, or None), `note_contains` / `note_lacks`, `pending_after`,
`task_after`, `card_after`, `remembered_after`, `list_after`, `route_contains`, `card_has` / `card_lacks` (model-first state
card), `no_write` (default True: nothing is written before Approve). Conversation-level `final_db` checks: `appointment`,
`patient`, `patients_total`, `appointments_total`, `no_writes`, `no_duplicate_patients`.

## Rollback

The live app's switch is Settings -> Command understanding (`intent_architecture` in `app_settings`, read at every
command). Setting it to Classic restores the original rules-first behaviour on the next sentence, no restart.
