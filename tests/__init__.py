import os

# Safety net: a developer's shell may export real Google Calendar settings. No
# test may ever pick them up (the sync engine is only ever driven by the fake
# client in tests/fake_gcal.py; see clinic/gcal_config.py).
os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)
# The app has no built-in calendar address (it comes from GOOGLE_CALENDAR_ID in
# .env), so the suite uses a neutral demo one -- never a developer's real one.
os.environ["GOOGLE_CALENDAR_ID"] = "clinic-demo@example.com"

# The intent router calls a local model (Ollama). Unit tests must never wait on
# it: switch it off here; the tests that exercise it patch the call or turn it
# on explicitly.
os.environ["INTENT_LLM_ENABLED"] = "0"
