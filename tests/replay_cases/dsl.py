"""The small vocabulary the replay conversations are written in (plain data, no behaviour).

A conversation is a dict built by `conv(...)`; its turns are built by:

  say(text, model=..., **expect)   the user says `text`. `model` is the golden planner answer for this turn
                                   (what a good model would call), used by the scripted configs; `expect` is a
                                   dict of checks (see scripts/replay/engine.py for every key).
  pick(index, **expect)            the user TAPS option `index` (0-based) of the open question.
  approve(**expect)                a person presses Approve on the card on screen (the same confirm path as /approve).
  idle(minutes)                    the clock advances; the 10-minute conversation memory may expire.
  network("down" | "up")           the planner becomes unreachable (or reachable again) for the next turns.
  architecture("classic" | "model_first" | "model_reads")   the Settings switch is flipped mid-conversation.

`model` is call("tool", arg=...), prose("words") (the model answered in plain words and called no tool), NO_CALL (the
model gave no tool call / was unavailable) or "timeout"; or a LIST of those, one per planner call of the turn (a
model-reads multi-step lookup: the first call is `sql_read(purpose="lookup")`, the next one sees its rows, a rejected
query is followed by its repair). Leaving `model` out means "the golden answer is not needed": the planner then
answers nothing.
"""

from tests.replay_cases.fixtures import BASE_SETUP

UNSET = object()
NO_CALL = None
TIMEOUT = "timeout"


def call(tool, **args):
    return (tool, args)


def prose(text):
    """The model replies in plain words, with no tool call (what Sarvam does with finish_reason "stop")."""
    return {"prose": text}


def say(text, model=UNSET, classic_model=UNSET, **expect):
    turn = {"say": text, "expect": expect}
    if model is not UNSET:
        turn["model"] = model
    if classic_model is not UNSET:
        turn["classic_model"] = classic_model
    return turn


def pick(index, **expect):
    return {"pick": index, "expect": expect}


def approve(**expect):
    return {"approve": True, "expect": expect}


def idle(minutes):
    return {"idle_minutes": minutes, "expect": {}}


def network(state):
    assert state in ("down", "up")
    return {"network": state, "expect": {}}


def architecture(mode):
    assert mode in ("classic", "model_first", "model_reads")
    return {"architecture": mode, "expect": {}}


def conv(id, title, category, language, turns, setup=None, final_db=None, known_gap=None, configs=None, note=None):
    """`configs`: run this conversation only under these configs (the others report SKIPPED), for behaviour that
    only exists in one architecture. `known_gap`: why it is expected to fail today (reported as KNOWN GAP)."""
    return {"id": id, "title": title, "category": category, "language": language, "turns": list(turns),
            "setup": setup if setup is not None else BASE_SETUP, "final_db": list(final_db or []), "known_gap": known_gap,
            "configs": list(configs) if configs else None, "note": note}
