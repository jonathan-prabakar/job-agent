"""Runner ownership, bounded child execution, schedule output and worker isolation."""

import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/job-run/scripts"))
import codex_runtime as runtime
import codex_workers as workers
import run_state


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="job agent ")
        self.ws = Path(self.tmp.name)
        (self.ws / "profile").mkdir()
        (self.ws / "state").mkdir()
        (self.ws / "profile/standing-answers.yaml").write_text("contact: {email: test@example.com}\n")
        self.prefs = self.ws / "profile/preferences.yaml"
        self.prefs.write_text("""browser:
  mode: dedicated
  port: 19444
  max_parallel_tabs: 2
  max_forms_per_run: 10
codex:
  execution: sequential
  browser_tools_approved: true
  runner_timeout_minutes: 1
  worker_timeout_minutes: 1
schedule:
  every_hours: 3
  jitter_minutes: 0
email:
  receipts: mock
auto_submit: true
dry_run: false
""")

    def tearDown(self):
        self.tmp.cleanup()


class RuntimeChecks(WorkspaceCase):
    def test_requires_dedicated_browser_and_explicit_approval(self):
        original = self.prefs.read_text()
        for changed in (original.replace("mode: dedicated", "mode: main"),
                        original.replace("browser_tools_approved: true", "browser_tools_approved: false")):
            self.prefs.write_text(changed)
            with self.assertRaises(ValueError):
                runtime.settings(self.ws)

    def test_invalid_worker_limit_is_rejected(self):
        self.prefs.write_text(self.prefs.read_text().replace("max_parallel_tabs: 2", "max_parallel_tabs: 0"))
        with self.assertRaises(ValueError):
            runtime.settings(self.ws)

    def test_active_managed_lock_cannot_be_stolen_or_unlocked_by_another_owner(self):
        lock = self.ws / "state/run.lock"
        run_state.take_lock(lock, owner_token="first")
        self.assertFalse(run_state.take_lock(lock, stale_hours=-1)["locked"])
        self.assertFalse(run_state.release_owned_lock(lock, "second"))
        self.assertTrue(lock.exists())
        self.assertTrue(run_state.release_owned_lock(lock, "first"))

    def test_worker_command_writes_only_its_folder_and_ignores_other_mcp_servers(self):
        folder = self.ws / "resumes/tailored/worker one"
        cmd = runtime.command(folder, folder / "result.json", 19444, worker=True)
        self.assertEqual(cmd[cmd.index("-C") + 1], str(folder))
        self.assertIn("--ignore-user-config", cmd)
        self.assertNotIn("--add-dir", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertTrue(any("--workspace=" + str(folder) in arg for arg in cmd))
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", cmd)
        self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", cmd)
        self.assertIn("sandbox_workspace_write.writable_roots=[]", cmd)

    def test_schedule_is_generated_without_activation_or_secret_environment(self):
        _, prefs, _ = runtime.settings(self.ws)
        output = self.ws / "schedule with spaces.plist"
        with patch.object(runtime.subprocess, "run", side_effect=AssertionError("must not activate")), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "not-to-be-copied"}):
            runtime.schedule_plist(self.ws, prefs, output)
        data = plistlib.loads(output.read_bytes())
        self.assertEqual(data["StartInterval"], 10800)
        self.assertFalse(data["RunAtLoad"])
        self.assertIn(str(self.ws), data["ProgramArguments"])
        self.assertNotIn("OPENAI_API_KEY", data["EnvironmentVariables"])
        with self.assertRaises(ValueError):
            runtime.schedule_plist(self.ws, prefs, output)

    def test_disabled_schedule_cannot_generate_or_run(self):
        self.prefs.write_text(self.prefs.read_text().replace("every_hours: 3", "every_hours: 0"))
        with self.assertRaises(ValueError):
            runtime.run_once(self.ws, scheduled=True)

    def test_failure_and_timeout_release_only_the_owned_lock(self):
        for rc in (1, 124):
            with patch.object(runtime, "ensure_browser"), patch.object(run_state, "screen_locked", return_value=False), \
                    patch.object(runtime, "execute", return_value=rc):
                result = runtime.run_once(self.ws)
            self.assertEqual(result["exit_code"], rc)
            self.assertFalse((self.ws / "state/run.lock").exists())

    def test_dryrun_is_passed_in_prompt_without_mutating_preferences(self):
        original = self.prefs.read_text()
        with patch.object(runtime, "ensure_browser"), patch.object(run_state, "screen_locked", return_value=False), \
                patch.object(runtime, "execute", return_value=0) as execute:
            result = runtime.run_once(self.ws, dry_run=True, scheduled=True)
        self.assertEqual(result["status"], "completed")
        self.assertIn("Explicit dry-run", execute.call_args.args[1])
        self.assertEqual(original, self.prefs.read_text())
        self.assertFalse((self.ws / "state/run.lock").exists())

    def test_browser_port_owned_by_another_profile_is_not_reused(self):
        with patch.object(runtime.launch_chrome, "port_alive", return_value=True), \
                patch.object(runtime.launch_chrome, "profile_owns_port", return_value=False):
            with self.assertRaises(ValueError):
                runtime.ensure_browser(self.ws, 19444)

    def test_child_timeout_terminates_the_process(self):
        folder = self.ws / "child"
        rc = runtime.execute([sys.executable, "-c", "import time; time.sleep(30)"], "", folder,
                             dict(os.environ), 0.1)
        self.assertEqual(rc, 124)

    def test_busy_runner_does_not_start_codex(self):
        run_state.take_lock(self.ws / "state/run.lock", owner_token="other")
        with patch.object(runtime, "execute") as execute:
            result = runtime.run_once(self.ws)
        self.assertEqual(result["status"], "busy")
        execute.assert_not_called()

    def test_coordinator_cannot_release_or_replace_the_native_lock(self):
        def replace_lock(*args):
            run_state.release_lock(self.ws / "state/run.lock")
            run_state.take_lock(self.ws / "state/run.lock", owner_token="replacement")
            return 0

        with patch.object(runtime, "ensure_browser"), patch.object(run_state, "screen_locked", return_value=False), \
                patch.object(runtime, "execute", side_effect=replace_lock):
            result = runtime.run_once(self.ws)
        self.assertIn("ownership changed", result["reason"])
        owner = json.loads((self.ws / "state/run.lock/owner.json").read_text())
        self.assertEqual(owner["runner_token"], "replacement")

    def test_second_blocked_run_uses_the_configured_notification(self):
        (self.ws / "state/notify.json").write_text('{"method":"none"}')
        with patch.object(run_state, "screen_locked", return_value=True), \
                patch.object(runtime.subprocess, "run") as notify:
            notify.return_value.returncode = 0
            runtime.run_once(self.ws)
            self.assertEqual(notify.call_count, 0)
            runtime.run_once(self.ws)
            self.assertEqual(notify.call_count, 1)

    def test_interruption_records_outcome_and_releases_owned_lock(self):
        with patch.object(runtime, "ensure_browser"), patch.object(run_state, "screen_locked", return_value=False), \
                patch.object(runtime, "execute", side_effect=KeyboardInterrupt):
            result = runtime.run_once(self.ws)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["exit_code"], 130)
        self.assertFalse((self.ws / "state/run.lock").exists())


class WorkerChecks(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.prefs.write_text(self.prefs.read_text().replace("execution: sequential", "execution: parallel"))
        self.items = []
        for n in range(4):
            jd = self.ws / f"state/jd-{n}.txt"
            jd.write_text("Software Engineer Intern")
            self.items.append({"job_key": str(n), "state": "QUEUED", "jd_path": str(jd),
                               "apply_url": f"http://127.0.0.1:18999/gh/company/jobs/{n}"})
        self.queue = self.ws / "state/queue.json"
        self.queue.write_text(json.dumps({"entries": self.items, "removed": []}))
        run_state.take_lock(self.ws / "state/run.lock", owner_token="test-owner")

    def test_batch_enforces_limit_and_own_browser_commands_without_shared_writes(self):
        active = maximum = 0
        mutex = threading.Lock()
        folders = []
        before = self.queue.read_bytes()

        def fake_execute(cmd, prompt, folder, env, timeout):
            nonlocal active, maximum
            with mutex:
                active += 1
                maximum = max(maximum, active)
                folders.append(folder)
            time.sleep(0.03)
            item = json.loads((folder / "job.json").read_text())
            result = {"job_key": item["job_key"], "status": "parked", "reason": "captcha",
                      "tab_url": item["apply_url"], "resume_path": "", "report_path": ""}
            (folder / "result.json").write_text(json.dumps(result))
            with mutex:
                active -= 1
            return 0

        with patch.object(runtime, "ensure_browser"), patch.object(runtime, "execute", side_effect=fake_execute):
            result = workers.run_batch(self.ws, self.items, owner_token="test-owner")
        self.assertEqual(maximum, 2)
        self.assertEqual(len(set(folders)), 4)
        self.assertEqual(len(result), 4)
        self.assertEqual(before, self.queue.read_bytes())

    def test_dispatch_rejects_jobs_not_in_the_gated_queue(self):
        item = dict(self.items[0], apply_url="https://example.com/another-job")
        with patch.object(runtime, "ensure_browser") as browser:
            with self.assertRaises(ValueError):
                workers.run_batch(self.ws, [item], owner_token="test-owner")
        browser.assert_not_called()

    def test_invalid_worker_output_becomes_parked_and_other_jobs_continue(self):
        with patch.object(runtime, "ensure_browser"), patch.object(runtime, "execute", return_value=1):
            result = workers.run_batch(self.ws, self.items[:2], owner_token="test-owner")
        self.assertEqual([r["status"] for r in result], ["parked", "parked"])

    def test_filled_report_requires_gated_pdf_and_matching_tab(self):
        folder = self.ws / "worker"
        folder.mkdir()
        pdf = folder / "resume.pdf"
        pdf.write_bytes(b"%PDF-test")
        (folder / "resume.pdf.gate.json").write_text(json.dumps({
            "verdict": "PASS", "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}))
        detail = folder / "report.md"
        detail.write_text("filled")
        report = {"job_key": "0", "status": "filled", "reason": "", "resume_path": str(pdf),
                  "report_path": str(detail), "tab_url": self.items[0]["apply_url"]}
        workers.validate_report(report, self.items[0], folder)
        with self.assertRaises(ValueError):
            workers.validate_report(dict(report, tab_url=report["tab_url"] + "evil"), self.items[0], folder)
        pdf.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            workers.validate_report(report, self.items[0], folder)


if __name__ == "__main__":
    unittest.main()
