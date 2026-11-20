# Codex setup

This fork exposes all ten job-agent skills to Codex, provides runtime-aware setup and processes jobs sequentially. Validate the browser workflow against the local mock ATS before a real dry run. The native local runner also supports opt-in scheduled runs and isolated fill workers. Setup checks alone do not mean that an application run has passed.

## Open the repository

```bash
git clone https://github.com/jonathan-prabakar/job-agent.git
cd job-agent
codex
```

In the desktop app, open the cloned folder as the project instead. Codex reads `AGENTS.md` and discovers the relative skill-folder links under `.agents/skills/`. The original `skills/` folders remain the source of truth; keep scripts, references and assets together. Do not install a second copy of the same skills globally.

In Codex CLI, use `/skills` to check that all ten skills appear, then ask:

```text
$job-setup
```

In a client without that selector, ask: "Set up job-agent using the job-setup skill." A request to review or modify the repository should not start the wizard.

Official references: [skill discovery](https://learn.chatgpt.com/docs/build-skills) and [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

## Prerequisites

- Node.js 20.19 or later, Python 3.9 or later, and Chrome 144 or later.
- `tectonic` and PyMuPDF for the resume build and mandatory upload gate. Check `tectonic --version` and `python3 -c "import fitz"`.
- On Windows, use `py -3` instead of `python3`. Windows and Linux remain community-supported.

Install commands are in [Getting started](getting-started.md). A missing PDF dependency blocks the resume gate; do not upload an unchecked PDF.

## Configure the browser

Choose the data workspace before registering the server: use `JOB_AGENT_HOME` when set, otherwise `~/.job-agent`. User profiles and resumes belong there, outside tracked source files. Resolve this to an absolute path and use that same path for browser file access. The Codex sandbox must also permit writes to the workspace; MCP file roots and Codex write permissions are separate checks.

First inspect `codex mcp list`. Reuse a matching configuration, and do not replace a server used by another project silently. The following is a template: replace `/absolute/path/to/job-agent-data` before running it.

```bash
codex mcp add job-agent-browser -- \
  npx -y chrome-devtools-mcp@1.10.1 \
  --autoConnect \
  --no-usage-statistics \
  --workspace=/absolute/path/to/job-agent-data
```

Open `chrome://inspect/#remote-debugging` in Chrome and turn on remote debugging. Reopen the Codex session if the tools have not loaded. Ask the agent to call `list_pages`, then click Chrome's **Allow remote debugging?** prompt yourself. A successful result proves attachment, not the complete application workflow. The first milestone does not install Claude or copy `~/.claude/agents/job-applier.md`.

For a separate Chrome profile, launch `skills/job-setup/scripts/launch_chrome.py` with `JOB_AGENT_HOME` set to the chosen workspace. Register the dedicated server instead:

```bash
codex mcp add job-agent-dedicated -- \
  npx -y chrome-devtools-mcp@1.10.1 \
  --browser-url=http://127.0.0.1:9333 \
  --no-usage-statistics \
  --workspace=/absolute/path/to/job-agent-data
```

Set `browser.mode: dedicated` in preferences and use only that server for browser work. Sign in once in the separate profile. Keep the existing package version pinned during migration.

Alternatively, put the stdio server's `command` and `args` in a `[mcp_servers.job-agent-browser]` table in a trusted project's `.codex/config.toml`. Generate machine-specific paths locally rather than checking them into this repository. See [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).

## Check connectors and finish setup

Discover connectors in the active client. Verify Gmail with one read-only label-list or search call. If it is missing, connect a Gmail plugin or authenticated MCP server in that client. A desktop connection does not prove that a separate scheduled CLI process can access it. Do not use Claude's connector settings.

Start with the local CSV tracker. Only offer Sheets or Notion after verifying access to the selected backend. Missing Gmail must stay visible as an incomplete setup capability: the prior-application check fails closed and must not be disabled to force a pass.

Continue the setup wizard for the resume, preferences, standing answers and writing voice. Preserve `setup-progress.json` on reruns. Keep auto-submit off, credential mode off and scheduling disabled unless an applicable, validated workflow and the user's settings authorize them. For unattended execution, use the native runner below after checking its own connector access.

## Validation before a real run

Run the discovery checks from the repository root:

```bash
python3 -m unittest discover -s tests -p test_codex_discovery.py
```

Run the opt-in [Codex integration runner](../tests/e2e/README.md#codex-browser-validation) in a new test workspace. It uses fake data, a throwaway headless Chrome and a process-local MCP configuration. It does not change your saved browser connection. Dry-run validation checks the live forms independently through MCP; mock-submit validation checks receipts and the status-sync chain.

After local validation passes, ask `$job-run in dry-run mode, one job at a time` for one real posting. Codex uses one browser connection and completes each job's tailoring, fill, live read-back and tracking before moving on. A saved `max_parallel_tabs` above 1 does not enable Codex workers. With `written_answers: writer`, the current agent drafts the answers in a separate pass. Missing required facts park that job for the summary rather than blocking other applications.

A dry-run request overrides saved `dry_run: false` for that run without editing the profile. The agent records Ready rows and a read-back file, leaves forms open at Submit, and never creates a submission or removes a queue entry without a receipt. Do not infer success on real ATS websites from a mock test.

## Local scheduled runner

The runner executes once per invocation. It does not install a background service on its own. Complete setup, verify the dedicated profile's logins and the CLI's email/tracker access, then record the approval explained by setup:

```yaml
browser:
  mode: dedicated
  port: 9333
codex:
  execution: sequential
  browser_tools_approved: true
  runner_timeout_minutes: 60
  worker_timeout_minutes: 20
schedule:
  every_hours: 3
  jitter_minutes: 15
```

Merge these fields into the existing profile; do not replace its other preferences. `browser_tools_approved` authorizes browser tool calls for this invocation only; saved Codex settings are unchanged. It does not enable auto-submit, account creation or credential mode. Submission still follows `auto_submit`, `dry_run`, caps and all form guards.

CSV is the validated unattended tracker. Real Gmail, Sheets and Notion connections need their own CLI authentication and tool approvals; a desktop connection or a successful read does not prove that a scheduled tracker write can succeed. Test the selected tracker's write access in a test tracker before enabling a schedule. The runner does not broaden email or external tracker permissions, and required prior-application checks still fail closed.

From the source checkout, run a practice cycle using the same Python environment that has PyMuPDF:

```bash
python3 skills/job-run/scripts/codex_runtime.py run --workspace /absolute/data/workspace --dry-run
```

Omit `--dry-run` to follow saved submission preferences. Add `--scheduled` only when a positive interval is configured; it also applies jitter. The computer must remain awake. A locked screen or inaccessible browser blocks the run; the runner never unlocks the computer. Only the dedicated profile's owned browser port is accepted. Logs, the final message and process outcome are kept under `state/codex-runs/<run-id>/`. A process exit of zero means Codex finished; read the final report for parked jobs or unavailable sources.

The runner holds the shared run lock across all phases. Another invocation exits busy rather than overlapping. Active managed locks cannot be stolen based on age alone; failures and timeouts release only the owned lock. Child process groups are bounded by the configured timeout. Chrome stays open so Ready and parked forms survive the run. Do not change the profile while a run is active.

For a Codex app automation request, use the host's Automations tools when available. The CLI below generates an operating-system schedule file; it is a separate integration. Do not register both schedulers for the same workspace.

On macOS, generate a launchd definition:

```bash
python3 skills/job-run/scripts/codex_runtime.py schedule --workspace /absolute/data/workspace --output /absolute/data/workspace/state/job-agent.plist
```

The file records absolute Python/source paths, a minimal PATH and the interval; it contains no tokens and has `RunAtLoad: false`. Keep the source checkout and Python environment at those paths. Generating it does not install or start it. When the user requests activation, install it as their per-user LaunchAgent and load it with `launchctl bootstrap gui/<uid> <installed plist>`. Stop it with `launchctl bootout gui/<uid> <installed plist>`. Do not run a second scheduler for the same workspace.

Linux cron and Windows Task Scheduler can invoke the same absolute Python/script command with `run --workspace <path> --scheduled`. Configure the interval in that scheduler to match the profile. These integrations are community-supported; the generated launchd file is macOS-specific. Use a local scheduler because a remote routine cannot drive this Chrome profile.

## Optional isolated workers

Set `codex.execution: parallel` to enable workers **through the native runner**. `browser.max_parallel_tabs` bounds concurrency (1 to 8) and `max_forms_per_run` bounds the batch. Use a data workspace outside the source checkout for parallel mode. Direct `$job-run` chats remain sequential and do not start worker processes.

The native runner first asks a coordinator to scan and gate. It then starts one Codex process per queued job, each with its own browser MCP process and unique writable output folder. Workers ignore saved MCP configuration and use the default CLI authentication/model; custom provider configurations are not propagated. They read the shared profile/master but cannot write shared state through their shell sandbox. Their browser file-access root is their output folder. Shared `/tmp` and `$TMPDIR` write roots are excluded; each worker gets private temp/cache directories inside its own folder.

Workers never submit, notify, mutate the master, or update the queue/tracker. They return structured filled/parked reports. The coordinator reconnects, finds the tabs by URL (page IDs are not shared), checks every live field and the exact gated PDF itself, then performs any allowed final click and tracking serially. Worker crashes or timeouts become parked reports, and other jobs continue. Browser contexts share the dedicated profile's cookies; the isolation is separate MCP processes, page ownership and file-write scope, not separate accounts or a protection boundary against a malicious worker.

Validate before using this mode on real applications:

```bash
python3 tests/e2e/codex_run.py --workspace /tmp/job-agent-parallel-test --mode dry-run --parallel
```

Use a new directory for each test. `--runtime` without `--parallel` tests the native runner's sequential path. Both options exercise its scheduled invocation path with zero jitter, without installing any scheduler. The default test remains the part-two direct CLI path. Real Gmail/Notion/Sheets access must be checked separately in the intended scheduled environment.
