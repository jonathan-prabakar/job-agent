#!/usr/bin/env python3
"""Fill a gated batch with separate Codex processes and separate browser MCPs.

Workers only write in their assigned output directory and never submit. The
calling coordinator owns queue/tracker updates and final browser read-back.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import uuid

import codex_runtime as runtime

SCHEMA = runtime.REPO / "skills/job-run/assets/worker-result.schema.json"
sys.path.insert(0, str(runtime.REPO / "skills/tailor-resume/scripts"))
import resume_gate


def contained(path, root):
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def validate_report(report, item, folder):
    if not isinstance(report, dict) or report.get("job_key") != item["job_key"]:
        raise ValueError("worker returned a different job key")
    if report.get("status") not in ("filled", "parked"):
        raise ValueError("worker did not return filled or parked")
    for name in ("reason", "tab_url", "resume_path", "report_path"):
        if not isinstance(report.get(name), str):
            raise ValueError(f"worker omitted {name}")
    for name in ("resume_path", "report_path"):
        if report[name] and (not Path(report[name]).is_absolute() or not contained(report[name], folder)):
            raise ValueError("worker artifact path is outside its assigned folder")
    if report["status"] == "parked":
        if not report["reason"].strip():
            raise ValueError("parked worker omitted a reason")
        return report
    pdf = Path(report["resume_path"])
    if not pdf.is_absolute() or not contained(pdf, folder) or not resume_gate.verify(pdf)[0]:
        raise ValueError("worker resume is outside its folder or has no valid gate")
    detail = Path(report["report_path"])
    if not detail.is_absolute() or not contained(detail, folder) or not detail.is_file():
        raise ValueError("worker report is missing or outside its folder")
    expected, actual = urllib.parse.urlparse(item["apply_url"]), urllib.parse.urlparse(report["tab_url"])
    prefix = expected.path.rstrip("/")
    if actual.netloc != expected.netloc or not (actual.path == prefix or actual.path.startswith(prefix + "/")) or "/thanks" in actual.path:
        raise ValueError("worker tab URL does not match its application")
    return report


def fill_one(ws, cfg, item, batch_dir, deadline=None):
    folder = batch_dir / uuid.uuid4().hex
    folder.mkdir()
    (folder / "job.json").write_text(json.dumps(item, indent=2) + "\n")
    prompt = (f"Read {runtime.REPO}/AGENTS.md and the skill files at {runtime.REPO}/skills. "
              f"Handle exactly the job in {folder}/job.json. JOB_AGENT_HOME={ws}. "
              f"Your only writable output directory is {folder}; the workspace profile and master "
              "resume are read-only inputs. Resolve every skill/script path from the repository. "
              "Run tailor-resume then apply-fill with worker_mode: true and defer_submit: true. "
              "Put all resume sources, PDFs, gate images, reviews, defense sheets, answers and your "
              "detailed apply-fill report inside your output directory. Never modify the master, "
              "preferences, queue, tracker, locks, status files or voice profile. Never notify; "
              "return parked reasons to the coordinator. Never press a final control, regardless "
              "of auto_submit or dry_run. Never spawn another agent or start another browser process. "
              "Use only your job-agent-runtime MCP connection and your own new tab. Keep its URL "
              "and never use another worker's page ID. Do not close filled or parked tabs. Every "
              "free-text answer uses your-voice yourself; written_answers writer means a drafting pass "
              "in this same process (leave still leaves boxes empty). Check resume_gate.py --verify before any "
              "upload, and inspect the three gate PNGs. If anything cannot be done within your "
              "permissions, return parked with the exact reason and leave the tab open. The "
              "coordinator will read every live field back; report filled, never verified. "
              "Return JSON matching the supplied schema, with absolute resume_path/report_path "
              "and tab_url; parked may use empty paths when no artifact was created. ")
    env = dict(os.environ, JOB_AGENT_HOME=str(ws))
    for name in (".tmp", ".cache"):
        (folder / name).mkdir()
    env.update(TMPDIR=str(folder / ".tmp"), TMP=str(folder / ".tmp"), TEMP=str(folder / ".tmp"),
               XDG_CACHE_HOME=str(folder / ".cache"), PYTHONDONTWRITEBYTECODE="1")
    for key in ("JOB_AGENT_MANAGED_RUN_TOKEN", "JOB_AGENT_CODEX_PHASE", "JOB_AGENT_RUN_DIR"):
        env.pop(key, None)
    result = folder / "result.json"
    try:
        remaining = cfg["worker_timeout"] if deadline is None else min(cfg["worker_timeout"], deadline - time.monotonic())
        if remaining <= 0:
            raise ValueError("worker batch deadline reached")
        rc = runtime.execute(runtime.command(folder, result, cfg["port"], worker=True, schema=SCHEMA),
                             prompt, folder, env, remaining)
        if rc:
            raise ValueError("worker timed out" if rc == 124 else f"worker exited {rc}")
        report = validate_report(json.loads(result.read_text()), item, folder)
    except (OSError, ValueError, RuntimeError) as exc:
        report = {"job_key": item["job_key"], "status": "parked", "reason": str(exc),
                  "tab_url": "", "resume_path": "", "report_path": ""}
    report = dict(report, worker_dir=str(folder))
    (folder / "validated-result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def run_batch(workspace, batch, deadline=None, owner_token=None):
    ws, prefs, cfg = runtime.settings(workspace)
    if cfg["execution"] != "parallel":
        raise ValueError("parallel workers require codex.execution: parallel")
    owner = json.loads((ws / "state/run.lock/owner.json").read_text())
    if not owner.get("runner_token") or owner.get("pid") != os.getpid() or (
            owner_token is not None and owner.get("runner_token") != owner_token):
        raise ValueError("only the native runner that owns this run lock may dispatch workers")
    if not isinstance(batch, list) or not batch:
        raise ValueError("batch must be a nonempty JSON list from apply_queue.py next")
    keys = set()
    queue = json.loads((ws / "state/queue.json").read_text())
    if not isinstance(queue, dict) or not isinstance(queue.get("entries"), list):
        raise ValueError("queue must contain an entries list")
    queued = {e["job_key"]: e for e in queue.get("entries", [])}
    for item in batch:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k]
                                                for k in ("job_key", "apply_url", "jd_path")):
            raise ValueError("each batch item needs job_key, apply_url and jd_path")
        if item["job_key"] in keys:
            raise ValueError("duplicate job key in worker batch")
        keys.add(item["job_key"])
        saved = queued.get(item["job_key"], {})
        if saved.get("state") != "QUEUED" or any(saved.get(k) != item[k] for k in ("apply_url", "jd_path")):
            raise ValueError("worker batch does not match the gated queue")
        if item.get("state", "QUEUED") != "QUEUED":
            raise ValueError("workers may only fill QUEUED entries")
        url = urllib.parse.urlparse(item["apply_url"])
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError("invalid application URL")
        if not contained(item["jd_path"], ws) or not Path(item["jd_path"]).is_file():
            raise ValueError("job description must exist in the workspace")
    limit = (prefs.get("browser") or {}).get("max_forms_per_run", 10)
    if len(batch) > number_limit(limit):
        raise ValueError("batch exceeds browser.max_forms_per_run")
    runtime.ensure_browser(ws, cfg["port"])
    batch_dir = ws / "resumes/tailored" / ("codex-workers-" + uuid.uuid4().hex)
    batch_dir.mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        reports = list(pool.map(lambda item: fill_one(ws, cfg, item, batch_dir, deadline), batch))
    (batch_dir / "batch-results.json").write_text(json.dumps(reports, indent=2) + "\n")
    return reports


def number_limit(value):
    runtime.number(value, "browser.max_forms_per_run", 1, 100)
    if int(value) != value:
        raise ValueError("browser.max_forms_per_run must be an integer")
    return int(value)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", default=os.environ.get("JOB_AGENT_HOME", "~/.job-agent"))
    ap.add_argument("--batch", required=True)
    args = ap.parse_args(argv)
    try:
        reports = run_batch(args.workspace, json.loads(Path(args.batch).read_text()))
        print(json.dumps(reports, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
