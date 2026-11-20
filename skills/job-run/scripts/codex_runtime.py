#!/usr/bin/env python3
"""Run job-agent once through Codex, or generate a local macOS schedule file.

Generating a schedule never installs or activates it. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/job-gate/scripts"))
sys.path.insert(0, str(REPO / "skills/job-setup/scripts"))
import prefs_yaml
import launch_chrome
import run_state

sys.modules.setdefault("codex_runtime", sys.modules[__name__])
_ACTIVE = set()
_ACTIVE_LOCK = threading.RLock()


def stop_children():
    with _ACTIVE_LOCK:
        children = list(_ACTIVE)
    for proc in children:
        stop_process(proc)


def interrupted(signum, frame):
    stop_children()
    raise KeyboardInterrupt("Codex runner stopped")


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def settings(workspace):
    ws = Path(workspace).expanduser().resolve()
    prefs = prefs_yaml.load(ws / "profile/preferences.yaml")
    if not isinstance(prefs, dict):
        raise ValueError("preferences must be a mapping")
    cfg = prefs.get("codex") or {}
    browser = prefs.get("browser") or {}
    schedule = prefs.get("schedule") or {}
    if not all(isinstance(section, dict) for section in (cfg, browser, schedule)):
        raise ValueError("codex, browser and schedule must be mappings")
    if browser.get("mode") != "dedicated":
        raise ValueError("the local runner and parallel workers require browser.mode: dedicated")
    if cfg.get("browser_tools_approved") is not True:
        raise ValueError("approve dedicated browser operation in setup (codex.browser_tools_approved: true)")
    port = number(browser.get("port", 9333), "browser.port", 1, 65535)
    if int(port) != port:
        raise ValueError("browser.port must be an integer")
    execution = cfg.get("execution", "sequential")
    if execution not in ("sequential", "parallel"):
        raise ValueError("codex.execution must be sequential or parallel")
    if execution == "parallel" and (ws == REPO or REPO in ws.parents):
        raise ValueError("use sequential mode when the data workspace is inside the source checkout")
    workers = number(browser.get("max_parallel_tabs", 3), "browser.max_parallel_tabs", 1, 8)
    if int(workers) != workers:
        raise ValueError("browser.max_parallel_tabs must be an integer")
    return ws, prefs, {"port": int(port), "execution": execution, "workers": int(workers),
                       "stale_hours": number(schedule.get("lock_stale_hours", 3), "lock stale hours", 0.01, 168),
                       "timeout": number(cfg.get("runner_timeout_minutes", 60), "runner timeout", 1, 120) * 60,
                       "worker_timeout": number(cfg.get("worker_timeout_minutes", 20), "worker timeout", 1, 60) * 60}


def command(working_dir, output, port, *, worker=False, schema=None, isolated=False):
    browser_args = ["--prefer-offline", "-y", "chrome-devtools-mcp@1.10.1",
                    f"--browser-url=http://127.0.0.1:{port}",
                    "--no-usage-statistics", f"--workspace={working_dir}"]
    cmd = [shutil.which("codex") or "codex", "exec", "--ephemeral", "--json",
           "--sandbox", "workspace-write", "--skip-git-repo-check", "-C", str(working_dir),
           "-c", 'approval_policy="never"', "-c", "agents.enabled=false",
           "-c", "sandbox_workspace_write.network_access=true",
           "-c", 'mcp_servers.job-agent-runtime.command="npx"',
           "-c", "mcp_servers.job-agent-runtime.args=" + json.dumps(browser_args),
           "-c", "mcp_servers.job-agent-runtime.required=true",
           "-c", "mcp_servers.job-agent-runtime.startup_timeout_sec=60",
           "-c", 'mcp_servers.job-agent-runtime.default_tools_approval_mode="approve"',
           "-o", str(output)]
    if worker or isolated:
        cmd.append("--ignore-user-config")
    if worker:
        cmd += ["-c", "sandbox_workspace_write.writable_roots=[]",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true"]
    if schema:
        cmd += ["--output-schema", str(schema)]
    return cmd + ["-"]


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        proc.wait()


def execute(cmd, prompt, folder, env, timeout):
    """Bounded child lifetime; all MCP descendants share its process group."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "events.jsonl").open("w") as out, (folder / "stderr.log").open("w") as err:
        kwargs = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                text=True, env=env, **kwargs)
        with _ACTIVE_LOCK:
            _ACTIVE.add(proc)
        try:
            proc.communicate(prompt, timeout=timeout)
            return proc.returncode
        except subprocess.TimeoutExpired:
            stop_process(proc)
            return 124
        finally:
            try:
                stop_process(proc)
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(proc)


def ensure_browser(ws, port):
    profile = ws / "chrome-profile"
    if launch_chrome.port_alive(port):
        if not launch_chrome.profile_owns_port(profile, port):
            raise ValueError("browser port belongs to another profile; refusing to attach")
        return
    env = dict(os.environ, JOB_AGENT_HOME=str(ws))
    result = subprocess.run([sys.executable, str(REPO / "skills/job-setup/scripts/launch_chrome.py"),
                             "--port", str(port)], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Chrome did not start")


def run_once(workspace, *, dry_run=False, scheduled=False, isolated=False):
    ws, prefs, cfg = settings(workspace)
    if not (ws / "profile/standing-answers.yaml").is_file():
        raise ValueError("complete job-setup before running")
    schedule = prefs.get("schedule") or {}
    if scheduled:
        number(schedule.get("every_hours", 0), "schedule.every_hours", 0.01, 168)
    if isolated and (prefs.get("email") or {}).get("receipts") != "mock":
        raise ValueError("--isolated-config is only for a mock-inbox validation workspace")
    token = uuid.uuid4().hex
    lock = ws / "state/run.lock"
    taken = run_state.take_lock(lock, cfg["stale_hours"], owner_token=token)
    if not taken["locked"]:
        return {"status": "busy", "exit_code": 3, "reason": taken["reason"]}
    folder = ws / "state/codex-runs" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + token[:8])
    result = {"status": "blocked", "exit_code": 1, "run_dir": str(folder)}
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        folder.mkdir(parents=True)
        if scheduled:
            jitter = number(schedule.get("jitter_minutes", 15), "schedule.jitter_minutes", 0, 60)
            time.sleep(random.uniform(0, jitter * 60))
        if run_state.screen_locked():
            raise RuntimeError("screen locked")
        ensure_browser(ws, cfg["port"])
        prompt = (f"Read {REPO}/AGENTS.md, then follow {REPO}/skills/job-run/SKILL.md once. "
                  f"The repository and skill resources are at {REPO}; resolve script paths from there. "
                  f"JOB_AGENT_HOME={ws}. The Python runner owns state/run.lock with runner_token={token}. "
                  "Verify that token in owner.json, keep the lock and do not acquire or release it yourself. "
                  "This is a local noninteractive run with dedicated browser operation approved in preferences. "
                  "Use only job-agent-runtime for browser work. Use the configured email source and fail closed "
                  "when required connectors are missing. Park human-only steps without waiting. "
                  "Do not install schedules, change preferences, edit repository code or use agent-spawn tools. "
                  "The native runner starts workers outside this Codex process; never launch workers yourself. The coordinator alone updates "
                  "shared state, reviews live forms and presses any permitted final controls. ")
        if dry_run:
            prompt += "Explicit dry-run for this run only: never press a final control even if auto_submit is true. "
        if isolated:
            prompt += ("Authorized fake localhost integration test only; the mock queue and saved auto-submit "
                       "settings are authorized for this test, subject to any explicit dry-run override. "
                       "Never use real employers or accounts. ")
        env = dict(os.environ, JOB_AGENT_HOME=str(ws), JOB_AGENT_MANAGED_RUN_TOKEN=token)
        deadline = time.monotonic() + cfg["timeout"]

        def phase(label, instruction, final):
            phase_env = dict(env, JOB_AGENT_CODEX_PHASE=label, JOB_AGENT_RUN_DIR=str(folder))
            rc = execute(command(ws, final, cfg["port"], isolated=isolated),
                         prompt + instruction, folder / label, phase_env,
                         max(0.1, deadline - time.monotonic()))
            if rc == 0:
                try:
                    owner = json.loads((lock / "owner.json").read_text())
                except (OSError, ValueError) as exc:
                    raise RuntimeError("coordinator released the native runner's lock") from exc
                if owner.get("runner_token") != token:
                    raise RuntimeError("run lock ownership changed during the coordinator phase")
            return rc

        if cfg["execution"] == "parallel":
            rc = phase("prepare", "Run only the prepare phase: scan and gate, take the next batch, "
                       f"write the JSON list to {folder}/batch.json and stop before tailoring or filling. "
                       "Leave the managed lock held. Do not launch workers or send the final run banner.",
                       folder / "prepare.md")
            if rc:
                result.update(exit_code=rc, status="timeout" if rc == 124 else "failed")
                raise RuntimeError(f"prepare phase exited {rc}")
            batch = json.loads((folder / "batch.json").read_text())
            if not isinstance(batch, list):
                raise ValueError("prepare phase did not produce a batch list")
            from codex_workers import run_batch
            reports = run_batch(ws, batch, deadline=deadline, owner_token=token) if batch else []
            (folder / "worker-results.json").write_text(json.dumps(reports, indent=2) + "\n")
            rc = phase("review", f"Resume the review phase using {folder}/batch.json and "
                       f"{folder}/worker-results.json; use {folder}/prepare.md and the saved scan/gate "
                       "report for stage counts. Do not scan, gate or fill again. "
                       "For each filled report, locate the existing tab by URL and perform your own "
                       "live read-back, optional guarded submit and tracking. For parked reports, "
                       "record the reason, notify per preferences and continue. Then sync statuses "
                       "and finish the job-run summary. Do not close parked or Ready tabs.", folder / "final.md")
        else:
            rc = phase("sequential", "Run the full loop sequentially once.", folder / "final.md")
        result.update(status="completed" if rc == 0 else "timeout" if rc == 124 else "failed", exit_code=rc)
        if rc:
            raise RuntimeError(f"Codex exited {rc}; see run logs")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["reason"] = str(exc)
        blocked = run_state.record_blocked(ws / "state/run-log.md", str(exc))
        if blocked["notify"] and (ws / "state/notify.json").is_file():
            try:
                notice = subprocess.run([sys.executable, str(REPO / "skills/job-run/scripts/notify.py"),
                                         "--title", "job-agent", "--message", blocked["banner"]],
                                        env=dict(os.environ, JOB_AGENT_HOME=str(ws)),
                                        capture_output=True, timeout=10)
                if notice.returncode:
                    result["notification_error"] = "blocked-run notification failed"
            except (OSError, subprocess.SubprocessError):
                result["notification_error"] = "blocked-run notification failed"
    except KeyboardInterrupt:
        result.update(status="interrupted", exit_code=130, reason="Codex runner interrupted")
    finally:
        try:
            if folder.exists():
                (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        finally:
            try:
                stop_children()
            finally:
                run_state.release_owned_lock(lock, token)
                if previous_handler is not None:
                    signal.signal(signal.SIGTERM, previous_handler)
    return result


def schedule_plist(ws, prefs, output):
    """Return a launchd job definition without installing it or copying secrets."""
    hours = number((prefs.get("schedule") or {}).get("every_hours", 0), "schedule.every_hours", 0.01, 168)
    state = ws / "state"
    path = os.pathsep.join(dict.fromkeys([str(Path(shutil.which(exe) or sys.executable).parent)
                                        for exe in ("codex", "npx", "tectonic")] +
                                       ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    payload = {"Label": "io.github.jonathan-prabakar.job-agent",
               "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "run",
                                    "--workspace", str(ws), "--scheduled"],
               "WorkingDirectory": str(ws), "StartInterval": max(1, int(hours * 3600)),
               "RunAtLoad": False, "EnvironmentVariables": {"PATH": path, "JOB_AGENT_HOME": str(ws)},
               "StandardOutPath": str(state / "scheduler.log"),
               "StandardErrorPath": str(state / "scheduler-errors.log")}
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError("schedule output exists; choose a new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(plistlib.dumps(payload))
    return output


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    for action in ("run", "schedule"):
        p = sub.add_parser(action)
        p.add_argument("--workspace", default=os.environ.get("JOB_AGENT_HOME", "~/.job-agent"))
        if action == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--scheduled", action="store_true")
            p.add_argument("--isolated-config", action="store_true")
        else:
            p.add_argument("--output", required=True)
    args = ap.parse_args(argv)
    try:
        if args.action == "schedule":
            ws, prefs, _ = settings(args.workspace)
            print(json.dumps({"generated": str(schedule_plist(ws, prefs, args.output)), "installed": False}))
            return 0
        result = run_once(args.workspace, dry_run=args.dry_run, scheduled=args.scheduled,
                          isolated=args.isolated_config)
        print(json.dumps(result))
        return result["exit_code"]
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
