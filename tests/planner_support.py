"""Shared setup for the tool-calling planner tests: the three-branch voice test
bed, a fake planner backend, the planner switched ON, and a guard that fails any
test that would reach a live model. Not a test module itself."""
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_voice_branches import BranchVoiceCase  # noqa: E402

from clinic.nlu import planner  # noqa: E402


class PlannerCase(BranchVoiceCase):
    """Branch A (Dr. Mehta), B (Dr. Rao), C (Dr. Iyer); patients Rakesh Verma, Mohan Lal,
    Mohan Das, Sunita Devi; three appointments tomorrow at A. The keyword rules and the
    label picker are the old ones (picker mocked: `self.pick`), the planner is the real
    one running on `self.backend` (a FakeBackend you script)."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"INTENT_LLM_ENABLED": "1", "INTENT_PLANNER_ENABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.backend = planner.FakeBackend()
        planner.set_backend(self.backend)
        self.addCleanup(planner.set_backend, None)
        self.pick = Mock(return_value=None)
        for target, mock in (("clinic.nlu.parser.pick_intent", self.pick), ("clinic.nlu.parser.prefetch_name", Mock())):
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Nothing here may reach a real model: the name extractor and label picker are mocked,
        # and any other HTTP call to Ollama fails the test.
        self.live = Mock(side_effect=AssertionError("a test reached the live model"))
        for target in ("clinic.nlu.llm_slots.httpx.post", "clinic.nlu.intent_llm.httpx.post"):
            patcher = patch(target, self.live)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, tool, /, **args):
        """Script the backend to answer every command with this tool call."""
        self.backend.script = (tool, args)

    def log_rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM planner_log ORDER BY id")]

    def table_counts(self, *tables):
        return [self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables]
