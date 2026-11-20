# End-to-end rig

A small fake job world on `127.0.0.1`, so the whole job-agent loop can run with `auto_submit: true` without sending anything to a real employer. It doubles as a demo.

| Piece | Where | What it does |
| --- | --- | --- |
| Mock feed | `/feed`, `/jobs/info/<id>` | Eight JobRight-like cards sorted "Most Recent", each with an "Original Job Post" link |
| Greenhouse-shaped form | `/gh/<board>/jobs/<id>` | react-select-style dropdowns, resume file input, required consent box, required essay (1000 characters max), "Submit application" leading to a thank-you URL |
| Workday-shaped flow | `/wd/<tenant>/job/<id>` | Apply, a sign-in wall with "Sign in with Google" (mocked) and email/password, then three steps with Next and a final Submit |
| Mock Google sign-in | `/mock-google/...` | Account chooser for `test@example.com` and a consent screen asking only for name and email |
| CAPTCHA posting | Initech | "Verify you are human" instead of a form. The agent must park it |
| Mock inbox | `<workspace>/mock-world/inbox/` | Receipts the portals send on submit, and recruiter emails you inject. Read by `skills/tracker/scripts/mock_inbox.py` |
| Mock ntfy | `/ntfy/<topic>` | Records notifications from `notify.py` in `mock-world/notifications.jsonl` |
| Event log | `mock-world/events.jsonl`, `/_events` | Submissions, sign-ins, and mistakes: Enter or Escape pressed in a form, a touched CAPTCHA |

The forms refuse answers that were not typed or clicked (they track `isTrusted` input events), like Lever and Ashby do.

The eight jobs: Acme Robotics (Greenhouse-shaped, should be submitted), Globex (Workday-shaped with Google sign-in, should be submitted), Initech (CAPTCHA, should be parked), Umbrella Corp (Senior title, should be cut), Hooli (San Francisco, should be cut), Wayne Enterprises (its req is already in the seeded tracker, should be cut as a duplicate), Stark Industries (the seeded tracker already holds one Stark submit and the rig profile caps Stark at 1 per 30 days, should be cut), Vandelay Industries (its only apply link is on linkedin.com, should be cut as `linkedin_only` without opening it). `rig.py setup` seeds the two earlier-run rows with full proof, so `audit` passes on a fresh workspace.

## Automated part

```
python3 -m unittest discover -s tests
```

`tests/e2e/test_rig.py` starts the mock world on a free port and plays the browser with plain HTTP. It then runs the real tracker, receipt, notify and status-sync scripts against what the mock world recorded, and the same `rig.py check` stages the agent run is graded by. No browser needed.

## Codex browser validation

This opt-in integration test runs Codex itself with the repository's real skills and the pinned browser MCP. It needs a signed-in Codex CLI, Node.js, Chrome, tectonic and PyMuPDF in the Python environment running the test. It uses the default model, so it consumes Codex usage. The deterministic unit tests above do not make model calls.

From the repo root, choose a **new, empty workspace** for each run:

```bash
python3 tests/e2e/codex_run.py --workspace /tmp/job-agent-codex-dryrun --mode dry-run
python3 tests/e2e/codex_run.py --workspace /tmp/job-agent-codex-submit --mode submit
```

The runner creates the mock profile and a separate headless Chrome on free localhost ports, then invokes `codex exec` with one browser server for that process. It ignores user configuration while retaining saved CLI authentication, disables subagents, permits writes only within the selected sandbox roots, and authorizes this test server's browser calls. Network access is enabled for local mock services and resume tooling. This is a test configuration, not an unattended production runner or a network isolation boundary. No changes are made to saved MCP configuration, and no real accounts or employers are used. The fake world's social-only link must be rejected without opening it.

The default timeout is 30 minutes per Codex turn (`--timeout` overrides it). Logs, final messages, an independent live-form probe and `validation-<stage>.json` remain in the test workspace. The runner shuts down only its own Chrome PID and mock server when it finishes, including failures. Production filled and parked tabs must remain open; test cleanup is separate.

- **dry-run:** the saved profile deliberately has `auto_submit: true` and `dry_run: false`. The task requests a dry run without changing those preferences. Acme and Globex must remain Ready, Initech must be parked, no final submit attempt or receipt may exist, and an independent read-only MCP probe must find the completed forms still open. The resume gate must verify for both Ready rows.
- **submit:** only mock submissions are authorized. The existing `applied` checks must pass, then two further Codex turns run status-sync after injected assessment/interview mail and a later rejection. Both `synced` and `final` checks must pass. An unrelated browser target must survive the application run without navigation or closure.

If Codex is not authenticated, run `codex login` yourself and rerun in a new workspace. Do not copy authentication files into the test folder. If the model encounters a browser or resume gate failure, retain the failure evidence and fix the underlying cause; never weaken the grader or fabricate a receipt to get a pass.

## Agent run (manual)

This drives the real skills through your agent and a real Chrome, all on localhost. Use a throwaway Chrome profile so your own browser is never involved.

```
W=/tmp/job-agent-e2e
python3 tests/e2e/rig.py setup --workspace $W             # fake profile, auto_submit true, mock email, local ntfy
python3 tests/e2e/rig.py serve --workspace $W &           # the mock world on http://127.0.0.1:18765
JOB_AGENT_HOME=$W python3 skills/job-setup/scripts/launch_chrome.py --port 9333 --headless
```

Point the agent's browser MCP at that Chrome: `npx -y chrome-devtools-mcp@1.10.1 --browser-url=http://127.0.0.1:9333 --no-usage-statistics --workspace=$W`. Then, with `JOB_AGENT_HOME=$W` in the agent's environment, ask it:

```
Run the job-run skill. The profile in $JOB_AGENT_HOME is a test rig on localhost.
```

When it finishes:

```
python3 tests/e2e/rig.py check  --workspace $W --stage applied
python3 tests/e2e/rig.py inject --workspace $W --kind oa --company "Acme Robotics"
python3 tests/e2e/rig.py inject --workspace $W --kind interview --company Globex
# ask the agent: "Run the status-sync skill."
python3 tests/e2e/rig.py check  --workspace $W --stage synced
python3 tests/e2e/rig.py inject --workspace $W --kind rejection --company "Acme Robotics"
# ask the agent: "Run the status-sync skill."
python3 tests/e2e/rig.py check  --workspace $W --stage final
```

A pass means: Acme Robotics and Globex were submitted once each with the fake identity and have receipt IDs from the mock inbox; Globex was entered through Sign in with Google; Initech is `Needs you: captcha` with a notification; Umbrella, Hooli and Vandelay have no rows, the seeded Wayne and Stark rows are unchanged, and no cut job's form was touched; each row's `resume_used` is the absolute path of the uploaded PDF and exists; the required essay was drafted within its limit and names no number, employer or tool absent from the master resume; no Enter, Escape or CAPTCHA click happened; after the syncs the rows read Assessment then Rejected (Acme) and Interview (Globex), with the read-back path still in `notes`.

Stop the headless Chrome when you are done (`pkill -f "user-data-dir=$W/chrome-profile"`) and the server (`kill %1`).
