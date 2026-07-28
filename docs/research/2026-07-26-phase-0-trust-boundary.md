# Phase 0 Research: Trustworthy MCP Execution Boundary

**Date:** 2026-07-26
**Applies to:** `fcp-mcp` v0.2.1
**Research gate:** Revalidated against the release candidate
**Next revalidation:** Immediately before any authorized tag or publication

## Question

What should a production-quality local MCP server guarantee before it adds
workflow graphs, durable memory, or broader autonomy?

## Primary sources reviewed

1. [MCP 2025-11-25 schema reference](https://modelcontextprotocol.io/specification/2025-11-25/schema)
   - Tool results distinguish domain failures with `isError: true`.
   - `outputSchema` describes `structuredContent`.
   - Tool annotations provide `readOnlyHint`, `destructiveHint`,
     `idempotentHint`, and `openWorldHint`.
2. [MCP tool-annotations risk vocabulary](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)
   - Annotations can inform approvals and policy.
   - Annotations are hints, not enforcement.
   - Deterministic controls remain necessary.
3. [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
   and [v2 changes](https://py.sdk.modelcontextprotocol.io/v2/whats-new/)
   - The production SDK line is v1 on 2026-07-26.
   - SDK v2 and the 2026-07-28 protocol revision are still prerelease.
   - V2 replaces `FastMCP` with `MCPServer` and changes protocol and error
     behavior.
   - The official guidance says not to use v2 in production before its stable
     release and recommends an upper bound below v2 for v1 applications.
4. [Python `os.replace`](https://docs.python.org/3/library/os.html#os.replace)
   - A successful same-filesystem replacement is atomic on POSIX.
5. [Python `tempfile`](https://docs.python.org/3/library/tempfile.html)
   - Secure temporary-file creation avoids the race inherent in `mktemp`.
6. [PyPA entry-points specification](https://packaging.python.org/en/latest/specifications/entry-points/)
   - A console-script entry point calls a no-argument function and uses its
     integer return value as the process exit code.
7. Local macOS `osascript(1)` manual
   - Arguments following a script are delivered to its `run argv` handler.
   - This allows dynamic values to remain data instead of executable script
     source.
8. [OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
   - Local MCP servers need least-privilege filesystem scope.
   - Stdio limits network exposure but does not constrain host filesystem or
     application privileges.

## Repository evidence considered

- The server currently exposes 88 tools and five prompts from a single
  3,001-line module.
- All tools return text-shaped results, and no tool supplies MCP annotations.
- Direct runtime probes reproduced false success, unsafe overwrite,
  path-escape, prompt/schema drift, FCPXML 1.11 misclassification, and
  AppleScript/JXA source interpolation.
- The current dependency `mcp>=1.0.0` would admit the breaking v2 release.
- The clean baseline is 119 passing tests on Python 3.12 with MCP SDK 1.28.1.

## Decisions

### Adopt now

- Pin `mcp>=1.27,<2` for v0.2.1.
- Preserve no-argument stdio startup and existing public tool names.
- Add a real CLI and a structured `fcp_doctor` MCP tool.
- Separate input roots, output policy, live-control permission, automation
  execution, and transactional writes into deterministic services.
- Make every known domain failure produce `isError: true`.
- Pass AppleScript/JXA values through `run argv`.
- Annotate every tool, while enforcing safety independently of annotations.
- Validate documentation and prompt calls against the runtime catalog.
- Require atomic, validated, recoverable FCPXML commits.

### Defer deliberately

- MCP SDK v2 migration until its stable artifacts and migration guidance can
  be rechecked after 2026-07-28.
- Universal typed output conversion until v0.3.0 because changing all success
  payloads in a patch release would break consumers.
- Capability-profile catalog reduction until v0.3.0.
- Durable workflow graphs and run ledgers until the execution substrate is
  trustworthy.
- Project knowledge graph, RAG, model routing, and multi-agent supervision to
  an outer harness or a later, separately justified feature.

### Reject

- A full 88-tool rewrite in v0.2.1.
- Middleware-only safety that leaves direct writes and subprocess handling
  inside tool bodies.
- Shipping the prerelease MCP SDK v2 in a production package.
- Treating tool annotations or prompt instructions as enforcement.

## Research-driven acceptance implications

- An annotation test is necessary but not sufficient; path, live-control, and
  transaction enforcement need direct adversarial tests.
- A subprocess call is successful only when the return code and promised
  output are verified.
- A tool-domain error must remain visible to the model as an MCP tool error,
  not as successful prose and not as an opaque protocol failure.
- A validated temporary file must be created beside its destination before
  `os.replace`, otherwise cross-filesystem behavior can defeat atomicity.
- The release workflow must install and exercise the built wheel because an
  editable source test does not verify console-script or package metadata
  behavior.

## Revalidation triggers

Repeat targeted research before changing the design if any of these occur:

- MCP SDK v2 stable ships before v0.2.1 is complete.
- The 2026-07-28 MCP specification changes tool errors, annotations, or
  structured output.
- FastMCP v1 cannot advertise the package version or annotations without
  private SDK APIs.
- Atomic replacement or directory syncing behaves differently on a supported
  platform.
- A supported MCP client cannot consume the planned structured doctor output.

## Revalidation addendum: FastMCP v1 server identity

The pre-implementation API inspection on 2026-07-26 triggered the metadata
revalidation condition:

- `FastMCP` 1.28.1 has no documented `version` constructor parameter or public
  version setter.
- The public low-level `Server` accepts `version`, but FastMCP creates and owns
  that object behind a private `_mcp_server` attribute.
- When the low-level version is unset, `create_initialization_options()` uses
  the installed `mcp` distribution version for `serverInfo.version`.
- Official FastMCP v1 examples and documented properties expose name,
  instructions, website, icons, and settings, but not server software version.

Decision:

- Do not mutate `_mcp_server` or copy FastMCP internals.
- Keep the wire name `fcp-mcp`.
- Put `fcp-mcp v0.2.1` in server instructions.
- Report package and wire/SDK versions separately in CLI and doctor.
- Document the v1 wire-version limitation in release notes.
- Re-evaluate correct wire identity through the public `MCPServer` v2 API in
  Phase 1.

## Release-candidate revalidation: 2026-07-26

The completed candidate was checked again against current primary sources
before its final verification run.

### Version snapshot

- [PyPI](https://pypi.org/project/mcp/) reports MCP Python SDK `1.28.1` as
  the current stable release.
- [The official SDK releases](https://github.com/modelcontextprotocol/python-sdk/releases)
  and [v2 documentation](https://py.sdk.modelcontextprotocol.io/v2/) report
  `2.0.0b2` as the latest prerelease. Stable v2 is still targeted for
  2026-07-27; it has not shipped as of this revalidation.
- MCP `2025-11-25` remains the
  [latest stable protocol revision](https://modelcontextprotocol.io/specification/2025-11-25).
  `2026-07-28` is a
  [release-candidate draft](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28-RC),
  not a stable specification.

The v1 dependency bound remains correct for a patch release. Phase 1 should
recheck the actual stable SDK and protocol artifacts after they ship rather
than porting against the beta or draft.

### Guidance rechecked

- The stable [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
  still distinguishes malformed protocol requests from actionable tool
  execution failures reported with `isError: true`. The candidate's coded
  domain-error boundary matches that guidance.
- The SDK's [v2 overview](https://py.sdk.modelcontextprotocol.io/v2/whats-new/)
  and [migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)
  confirm that the migration is not a rename-only change: `FastMCP` becomes
  `MCPServer`, Python wire fields become snake_case, server identity becomes a
  public constructor concern, low-level validation/error behavior changes,
  and sync handlers move to worker threads. Deferring that migration remains
  the bounded choice for v0.2.1.
- Python's [`tempfile.mkstemp`](https://docs.python.org/3/library/tempfile.html#tempfile.mkstemp)
  still provides race-free secure creation and accepts an explicit directory.
  [`os.replace`](https://docs.python.org/3/library/os.html#os.replace) is atomic
  after a successful same-filesystem replacement and may fail across
  filesystems. The candidate creates, validates, flushes, and replaces its
  temporary FCPXML beside the destination.
- The [PyPA entry-points specification](https://packaging.python.org/en/latest/specifications/entry-points/)
  still defines a no-argument console function whose integer return becomes
  the process exit code. The installed-wheel smoke exercises that generated
  wrapper rather than relying on an editable install.
- Current [PyPA publishing guidance](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
  and the [PyPI Trusted Publishing security model](https://docs.pypi.org/trusted-publishers/security-model/)
  prefer short-lived OIDC credentials, a protected deployment environment,
  and a publish job limited to retrieving verified distributions and
  publishing them.

### Material change caused by revalidation

The tag workflow was changed from a stored `PYPI_API_TOKEN` to isolated PyPI
Trusted Publishing:

- the unprivileged `verify` job repeats the release gates, builds the
  distributions, smoke-tests the installed wheel, and uploads one short-lived
  artifact;
- the `publish` job receives `id-token: write` only at job scope and has
  exactly two operations: download that artifact and invoke the pinned PyPA
  publisher;
- the job is bound to the `pypi` GitHub environment.

Before anyone creates a release tag, maintainers must register
`.github/workflows/publish.yml` and environment `pypi` as the trusted publisher
for the PyPI project, configure required reviewers on that environment, and
protect `v*` tags. No external release configuration or publication was
performed during Phase 0.

### Revalidation decision

No source invalidated the Phase 0 execution-boundary design. The SDK v2 beta
and draft protocol make the planned Phase 1 research gate more important, but
do not justify expanding this patch into a migration. Subject to the clean
verification and the external release prerequisites above, the candidate is
ready for maintainer review.

## Release-candidate verification evidence

### Automated gates

The following evidence was collected locally on macOS with Python 3.12.13:

- Ruff passed over `src`, `tests`, and `scripts`.
- The contract checker validated 47 executable tool-call blocks across
  `WORKFLOWS.md` and `LLM_GUIDE.md`.
- The complete suite passed: 243 tests.
- Overall statement coverage was 64.46%, above the 60% floor.
- All 11 trust-boundary modules met the 90% per-module floor.
- `pip-audit --local` reported no known vulnerabilities. The local editable
  `fcp-mcp` distribution was explicitly skipped because v0.2.1 is not yet on
  PyPI; all installed third-party distributions were audited.
- Both GitHub Actions workflows passed `actionlint`.
- Wheel and source distribution passed `python -m build` and `twine check`.
- A new Python 3.12 environment installed only the built wheel plus resolved
  dependencies. Its stdio smoke initialized protocol `2025-11-25`, listed 89
  tools and five prompts, called `fcp_doctor`, and independently reported
  package `0.2.1`, SDK/wire `1.28.1`, and a non-blocking `degraded` status.

For a reproducible surface-size measurement, the 89 tool models were dumped
with Pydantic aliases, JSON mode, `None` fields excluded, compact separators,
and UTF-8 encoding. The serialized catalog is 70,330 bytes with SHA-256
`9746c4dbdae71994b3743b0717388791ab60c656cfb304e78c1a02957ac17559`.

### Adversarial trajectory matrix

| Required trajectory | Direct evidence | Result |
|---|---|---|
| Parent traversal and symlink escape | `test_parent_traversal_is_rejected`; `test_symlink_escape_is_rejected` | Pass |
| Same-file overwrite | `test_commit_rejects_same_source_and_destination` | Pass |
| Invalid XML preserves prior destination | `test_invalid_xml_never_replaces_existing_destination` | Pass; prior bytes unchanged and no temp remained |
| Hostile OSA value remains data | `test_dynamic_value_is_argv_not_script_source` | Pass |
| Missing media is an MCP error with no output | `test_missing_media_is_wire_error_and_creates_no_output` | Pass; `isError=true` |
| Undefined template contract is explicit | `test_apply_template_fails_honestly` | Pass; `unsupported_contract` |
| Package and wire versions are separate | `test_wire_identity_and_doctor_versions_are_truthful` | Pass |
| Disabled live control runs nothing | `test_live_control_disabled_runs_nothing` | Pass |

The nine focused tests above were also run as a separate adversarial batch;
all nine passed.

### Compatibility and design review

The runtime catalog at base commit
`a306424586955c1c5adc4a54ca7a196513ba10b1` was compared with the candidate:

- base tools: 88;
- candidate tools: 89;
- removed tools: none;
- added tools: `fcp_doctor`;
- existing required input properties removed: none;
- existing required inputs made optional: none.

Every Phase 0 completion criterion has command or test evidence. The one
accepted implementation limitation remains FastMCP v1's public wire-version
surface, disclosed above and verified by doctor. The separate disposable-FCP
import remains a pre-publication compatibility gate rather than a claim made
by structural validation.

### Optional live-FCP gate

Final Cut Pro was initially absent. With explicit user authorization it was
launched from the CLI using `open -a "Final Cut Pro"`. The live-control doctor
then reported:

- Final Cut Pro 12.2 installed and running;
- macOS Accessibility trust available;
- live control enabled for the isolated command;
- zero open libraries.

The read-only `fcp_get_app_state` query returned the same version, a false
`frontmost` state at query time, and `libraryCount: 0`. With no user library or
timeline open, the reversible UI exercise selected the Blade tool and restored
the Select tool in a `finally` block:

```json
{"exercise":"Tool: blade","restored":"Tool: select"}
```

This passes live application reachability, permission, read-only query, and
reversible automation without touching project data. It is not the separate
authoritative disposable-project FCPXML import gate required before
publication; no project mutation or import is claimed.
