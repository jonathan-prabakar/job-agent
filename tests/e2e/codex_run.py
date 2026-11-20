#!/usr/bin/env python3
"""Run Codex itself against the localhost rig in a fresh throwaway Chrome profile.

This is an opt-in integration test, never part of unittest discovery.
Requires Codex login, Node, Chrome, tectonic and PyMuPDF. Uses Codex's default
model and saved authentication; does not change user MCP configuration.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import urllib.request
import urllib.parse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import rig
import mcp_probe


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def codex_command(ws: Path, port: int, label: str) -> list[str]:
    args = ["--prefer-offline", "-y", "chrome-devtools-mcp@1.10.1",
            f"--browser-url=http://127.0.0.1:{port}",
            "--no-usage-statistics", f"--workspace={ws}"]
    return ["codex", "exec", "--ignore-user-config", "--ephemeral", "--json",
            "--sandbox", "workspace-write", "-C", str(REPO), "--add-dir", str(ws),
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", 'mcp_servers.job-agent-test.command="npx"',
            "-c", "mcp_servers.job-agent-test.args=" + json.dumps(args),
            "-c", "mcp_servers.job-agent-test.required=true",
            "-c", 'mcp_servers.job-agent-test.default_tools_approval_mode="approve"',
            "-c", "agents.enabled=false",
            "-c", "mcp_servers.job-agent-test.startup_timeout_sec=60",
            "-o", str(ws / f"{label}-final.md"), "-"]


def run_agent(ws: Path, port: int, label: str, prompt: str, timeout: int) -> None:
    env = dict(os.environ, JOB_AGENT_HOME=str(ws))
    with (ws / f"{label}-events.jsonl").open("w") as out, \
            (ws / f"{label}-stderr.log").open("w") as err:
        result = subprocess.run(codex_command(ws, port, label), input=prompt,
                                text=True, stdout=out, stderr=err, env=env,
                                timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Codex exited {result.returncode}; see {label}-stderr.log and events")


def require_check(ws: Path, stage: str) -> None:
    problems = rig.check(ws, stage)
    if (ws / "state" / "run.lock").exists():
        problems.append("run lock was not released")
    (ws / f"validation-{stage}.json").write_text(
        json.dumps({"stage": stage, "passed": not problems, "problems": problems}, indent=2) + "\n")
    if problems:
        raise RuntimeError("; ".join(problems))
    print(f"stage {stage}: PASS", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True, help="New or empty test-only directory")
    ap.add_argument("--mode", choices=("dry-run", "submit"), default="dry-run")
    ap.add_argument("--timeout", type=int, default=1800, help="Seconds per Codex turn")
    ap.add_argument("--runtime", action="store_true", help="Validate the production scheduled-runner path")
    ap.add_argument("--parallel", action="store_true", help="Use the native runner with isolated fill workers")
    args = ap.parse_args()
    ws = Path(args.workspace).expanduser().resolve()
    if ws.exists() and any(ws.iterdir()):
        ap.error("use a new or empty workspace; existing test evidence is never overwritten")
    for exe in ("codex", "npx", "tectonic"):
        if not shutil.which(exe):
            ap.error(f"missing executable: {exe}")
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        ap.error("install PyMuPDF in this Python environment first")
    ws.mkdir(parents=True, exist_ok=True)
    server = rig.mock_ats.make_server(0, ws / "mock-world")
    port = server.server_address[1]
    rig.setup(ws, port)
    browser_port = free_port()
    prefs = rig.ws_paths(ws)["prefs"]
    text = prefs.read_text().replace("  mode: main ", "  mode: dedicated ", 1)
    text = text.replace("  port: 9333 ", f"  port: {browser_port} ", 1)
    text = text.replace("python_cmd: python3 ", f"python_cmd: {json.dumps(sys.executable)} ", 1)
    if args.runtime or args.parallel:
        text = text.replace("  browser_tools_approved: false", "  browser_tools_approved: true", 1)
        text = text.replace("  every_hours: 0 ", "  every_hours: 3 ", 1)
        text = text.replace("  jitter_minutes: 15 ", "  jitter_minutes: 0 ", 1)
    if args.parallel:
        text = text.replace("  execution: sequential", "  execution: parallel", 1)
    prefs.write_text(text)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ, JOB_AGENT_HOME=str(ws))
    launcher = REPO / "skills/job-setup/scripts/launch_chrome.py"
    try:
        subprocess.run([sys.executable, str(launcher), "--port", str(browser_port),
                        "--headless"], env=env, check=True)
        # A pre-existing target must survive the application run untouched.
        sentinel_url = "data:text/html,<title>Unrelated tab</title><p>Leave this tab alone</p>"
        req = urllib.request.Request(
            f"http://127.0.0.1:{browser_port}/json/new?" +
            urllib.parse.quote(sentinel_url, safe=""), method="PUT")
        with urllib.request.urlopen(req, timeout=10) as response:
            sentinel = json.load(response)
        context = (f"Use the job-agent skills in this repository. JOB_AGENT_HOME is {ws}. "
                   "This is an authorized localhost integration test with a fake identity. "
                   "Use only the job-agent-test MCP browser and localhost sites; never connect "
                   "to real Gmail, employers, social sites or the user's normal Chrome. "
                   "Use the profile's python_cmd for all Python scripts. Do not edit repository "
                   "code, test fixtures, mock events, receipts or checks. Drive forms through "
                   "real browser tools; do not emulate submissions via HTTP or JavaScript. "
                   "Do not spawn subagents. ")
        mode = ("Run $job-run in dry-run mode. Override saved dry_run false for this run only."
                if args.mode == "dry-run" else
                "Run $job-run once. The mock queue and auto-submit are authorized for this test.")
        print(f"Running Codex {args.mode}; evidence: {ws}", flush=True)
        if args.runtime or args.parallel:
            sys.path.insert(0, str(REPO / "skills/job-run/scripts"))
            import codex_runtime
            result = codex_runtime.run_once(ws, dry_run=args.mode == "dry-run", scheduled=True, isolated=True)
            if result["exit_code"]:
                raise RuntimeError(result)
        else:
            run_agent(ws, browser_port, "run", context + mode, args.timeout)
        if prefs.read_text() != text:
            raise RuntimeError("the run changed saved preferences instead of using run-scoped settings")
        (ws / "live-forms.json").write_text(json.dumps(
            mcp_probe.inspect_forms(ws, browser_port), indent=2) + "\n")
        with urllib.request.urlopen(f"http://127.0.0.1:{browser_port}/json/list", timeout=10) as response:
            targets = json.load(response)
        if not any(t["id"] == sentinel["id"] and t["url"] == sentinel["url"] for t in targets):
            raise RuntimeError("unrelated tab was closed or navigated")
        require_check(ws, "dryrun" if args.mode == "dry-run" else "applied")
        if args.mode == "submit":
            rig.inject(ws, "oa", "Acme Robotics")
            rig.inject(ws, "interview", "Globex")
            run_agent(ws, browser_port, "sync", context + "Run $status-sync once.", args.timeout)
            require_check(ws, "synced")
            rig.inject(ws, "rejection", "Acme Robotics")
            run_agent(ws, browser_port, "final-sync", context + "Run $status-sync once.", args.timeout)
            require_check(ws, "final")
        return 0
    finally:
        # Only the PID started by this test profile is terminated.
        marker = ws / "chrome-profile" / "job-agent-chrome.json"
        if marker.exists():
            try:
                os.kill(json.loads(marker.read_text())["pid"], 15)
            except ProcessLookupError:
                pass
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
