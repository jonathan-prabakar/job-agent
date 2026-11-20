"""Negative controls for the browser-run grader, without invoking Codex."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import codex_run
import rig


class CodexRigChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        rig.setup(self.ws, 18999)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dryrun_rejects_a_final_click_even_when_submission_failed(self):
        errors = rig.check_dryrun(self.ws, {}, [{"kind": "submit_rejected"}])
        self.assertIn("dry run pressed a final submit control", errors)

    def test_dryrun_rejects_receipts(self):
        rig.mock_inbox.add(rig.ws_paths(self.ws)["inbox"], "no-reply@acme.mock",
                           "test@example.com", "Thank you for applying to Acme Robotics",
                           "We received your application for GH-1001.")
        self.assertIn("dry run produced email receipts", rig.check_dryrun(self.ws, {}, []))

    def test_ready_rows_without_live_evidence_cannot_pass(self):
        rows = {"Acme Robotics": {"status": "Ready"}, "Globex": {"status": "Ready"}}
        errors = rig.check_dryrun(self.ws, rows, [])
        self.assertIn("missing or unreadable independent live-form evidence", errors)
        (self.ws / "live-forms.json").write_text("[]")
        errors = rig.check_dryrun(self.ws, rows, [])
        self.assertTrue(any("no live form left open" in error for error in errors))

    def test_untrusted_js_fields_cannot_pass_as_a_ready_form(self):
        form = {"url": "http://127.0.0.1:18999/gh/acmerobotics/jobs/a1f0c0de0001",
                "body": "Acme Robotics Submit application", "fields": {
                    "first_name": "Test", "last_name": "Applicant", "email": "test@example.com",
                    "phone": "5555550100", "why_us": "I build services.",
                    "degree": "Bachelor of Science (BS)", "sponsorship": "No",
                    "how_heard": "Company careers page", "consent": True},
                "trusted": [], "resume": {"name": "resume.pdf", "size": 100}}
        (self.ws / "live-forms.json").write_text(json.dumps([form]))
        errors = rig.check_dryrun(self.ws, {}, [])
        self.assertTrue(any("incomplete or untrusted" in error for error in errors))

    def test_runner_scopes_browser_and_writes_to_the_test_workspace(self):
        command = codex_run.codex_command(self.ws, 19444, "test")
        self.assertIn("--ignore-user-config", command)
        self.assertIn(str(self.ws), command)
        self.assertTrue(any("--browser-url=http://127.0.0.1:19444" in item for item in command))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)


if __name__ == "__main__":
    unittest.main()
