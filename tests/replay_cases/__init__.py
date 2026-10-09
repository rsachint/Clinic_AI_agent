"""The replay conversation set: scripted multi-turn conversations with a golden planner answer for each turn and
the checks the app must pass (see dsl.py for the vocabulary and scripts/replay/engine.py for how they are scored).
Plain data, no behaviour. Every name and phone number is made up."""

from tests.replay_cases import (architecture_switch, dialogue, entities, missing_and_prose, multilingual, next_available, reads,
                                regressions, safety, sql_reads)

MODULES = (regressions, dialogue, entities, multilingual, reads, safety, architecture_switch, missing_and_prose, next_available,
           sql_reads)
ALL_CASES = [case for module in MODULES for case in module.CASES]
