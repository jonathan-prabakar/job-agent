# Codex workflow validation

## Part two: sequential execution

Validated on November 19, 2026 using macOS, Codex CLI `0.167.0-alpha.3.2`, Chrome `157.0.8103.79`, the pinned `chrome-devtools-mcp@1.10.1`, Python `3.14.3`, tectonic and PyMuPDF `2.1`. Codex used saved ChatGPT authentication and its default model. No real employer, inbox or normal browser profile was used.

| Check | Result |
| --- | --- |
| Full automated suite | 215 tests passed, none skipped |
| Real Codex mock-submit run | Eight postings scanned; expected three passes; Acme and Globex submitted once each; Initech parked at CAPTCHA |
| Input and upload | Mock forms accepted trusted typing/clicks and attached PDFs; resume gates passed; generated resume images visually reviewed |
| Tracking | Matching mock receipts, absolute PDF paths and saved read-backs; tracker audit passed |
| Status sync through Codex | Acme Assessment then Rejected; Globex Interview; read-back notes retained |
| Real Codex dry run | Two complete Ready forms, CAPTCHA tab left open, zero final submit attempts and zero receipts |
| Independent dry-run probe | Read-only MCP inspection confirmed live fields, trusted input, attached filename/size, Workday review and final controls |
| Saved settings | Dry-run task overrode saved auto-submit settings without editing preferences |
| Tab and lock discipline | Unrelated target survived both runs; run locks released |

The opt-in runner and reproduction commands are in [the rig documentation](../tests/e2e/README.md#codex-browser-validation). The first CLI attempt exposed a missing tool-approval setting; the runner now authorizes only its throwaway browser server for the current invocation and leaves saved MCP configuration alone.

These checks establish the local mock workflow. They do not establish compatibility with every real ATS, Gmail connection, operating system or Chrome attachment mode. Start real use with one interactive dry run that stops at Submit. The part-three checks below cover the native runner and isolated workers.

## Part three: native runner and isolated workers

Completed November 20, 2026 in the same macOS/Codex/Chrome/Python environment described above.

| Check | Result |
| --- | --- |
| Full automated suite | 233 tests passed, none skipped |
| Native scheduled-invocation path with parallel dry-run | PASS: two complete Ready forms, parked CAPTCHA, no final submissions or receipts, settings unchanged and lock released |
| Native parallel mock-submit run | PASS: coordinator submitted Acme and Globex once each after worker handoff; receipts and tracker audit matched |
| Status sync after parallel run | PASS: applied, synced and final stages, including later Acme rejection and retained Globex interview |
| Worker file scope through actual Codex | Own output writable; sibling shared-state and temporary folders denied |
| Worker controls | Separate Codex/MCP processes, unique folders, gated PDFs and structured reports; final review and shared-state writes owned by the coordinator |
| Ownership/failure controls | Tests cover active-lock protection, replacement-owner safety, busy runs, failures, timeouts, interruptions and second-blocked-run notifications |
| macOS schedule definition | Generated without installation; `plutil -lint` passed; no scheduler enabled |

Validation exposed a browser-package startup timeout in the independent probe. Browser commands now prefer the pinned cached package, retaining normal download behavior on a cache miss. The complete mock submission/status pipeline passed after that change. Shared temporary write roots are explicitly excluded for workers, which use private temp/cache folders.

These are local mock tests with CSV and mock email. Real Gmail, Sheets/Notion write approvals and real ATS compatibility remain environment-specific checks before enabling personal scheduled runs. No real applications or account data were used, and no recurring service was activated.
