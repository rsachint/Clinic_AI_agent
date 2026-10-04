import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import gcal_config, notify


class ScratchInstanceGuardTests(unittest.TestCase):
    """A scratch/test copy of the app (its own CLINIC_DB_PATH) must never reach
    the real Google calendar or message real patients."""

    def setUp(self):
        self.key = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.key.close()
        self.addCleanup(os.unlink, self.key.name)

    def env(self, **extra):
        base = {"GOOGLE_SERVICE_ACCOUNT_FILE": self.key.name}
        base.update(extra)
        return patch.dict(os.environ, base, clear=False)

    def test_the_real_database_still_syncs(self):
        with self.env():
            os.environ.pop("CLINIC_DB_PATH", None)
            self.assertTrue(gcal_config.is_configured())
        with self.env(CLINIC_DB_PATH="clinic.db"):
            self.assertTrue(gcal_config.is_configured())
        with self.env(CLINIC_DB_PATH="/srv/clinic-copilot/clinic.db"):
            self.assertTrue(gcal_config.is_configured())

    def test_a_scratch_database_never_counts_as_configured(self):
        with self.env(CLINIC_DB_PATH="/tmp/scratch-demo.db"):
            self.assertTrue(gcal_config.uses_scratch_database())
            self.assertFalse(gcal_config.is_configured())

    def test_the_escape_hatch_is_explicit(self):
        with self.env(CLINIC_DB_PATH="/tmp/scratch-demo.db", GOOGLE_SYNC_ALLOW_SCRATCH_DB="1"):
            self.assertTrue(gcal_config.is_configured())

    def test_a_scratch_database_is_always_dry_run_for_whatsapp(self):
        with patch.dict(os.environ, {"WHATSAPP_NOTIFY_MODE": "live", "CLINIC_DB_PATH": "/tmp/scratch-demo.db"}):
            self.assertEqual(notify.notify_mode(), "dry_run")
            sender, dry = notify.resolve_sender()
            self.assertIsNone(sender)
            self.assertTrue(dry)

    def test_the_real_database_keeps_the_configured_whatsapp_mode(self):
        with patch.dict(os.environ, {"WHATSAPP_NOTIFY_MODE": "live"}):
            os.environ.pop("CLINIC_DB_PATH", None)
            self.assertEqual(notify.notify_mode(), "live")


if __name__ == "__main__":
    unittest.main()
