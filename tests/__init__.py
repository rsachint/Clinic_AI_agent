import os

# Safety net: a developer's shell may export real Google Calendar settings. No
# test may ever pick them up (the sync engine is only ever driven by the fake
# client in tests/fake_gcal.py; see clinic/gcal_config.py).
os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)
# The app has no built-in calendar address (it comes from GOOGLE_CALENDAR_ID in
# .env), so the suite uses a neutral demo one -- never a developer's real one.
os.environ["GOOGLE_CALENDAR_ID"] = "clinic-demo@example.com"
# Google sync is off by default in the app; its own tests exercise it with a fake client.
os.environ["GOOGLE_CALENDAR_SYNC"] = "1"

# The intent router calls a local model (Ollama). Unit tests must never wait on
# it: switch it off here; the tests that exercise it patch the call or turn it
# on explicitly.
os.environ["INTENT_LLM_ENABLED"] = "0"
# Same for the tool-calling planner (clinic/nlu/planner.py): the suite never calls
# it live. The tests of the planner itself use a fake backend and switch it on.
os.environ["INTENT_PLANNER_ENABLED"] = "0"
# And the hosted planner (clinic/nlu/sarvam.py): a developer's .env may select it, and no test may reach Sarvam.
# The tests of the backend itself use a fake HTTP transport and set what they need.
os.environ["PLANNER_BACKEND"] = "local"
# The idle internet check (clinic/network_health.py) opens real TCP connections; no test may. The tests of the
# check itself inject fake targets and a fake connect.
os.environ["NETWORK_PROBE_ENABLED"] = "0"
# The built-in default of the command-understanding switch is the model-first mode with model-written reads
# (clinic/architecture.py). The suite keeps testing the classic routing unless a test sets its own mode; the tests of
# the switch itself remove this variable to check the real default.
os.environ["INTENT_ARCHITECTURE"] = "classic"
