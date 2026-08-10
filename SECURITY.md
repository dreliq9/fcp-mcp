# Security policy

## Supported release

Security fixes are evaluated for the current supported release: **fcp-mcp
0.3.0**. Include the exact installed version in every report.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability. Private GitHub
vulnerability reporting is not asserted as an available channel for this
repository. Instead, send a private report to `dreliq9@gmail.com` with the
subject `fcp-mcp security report`. Include a minimal reproduction, affected
version, impact, and any suggested containment. Do not include private media,
unredacted FCPXML, access tokens, or `doctor --json` output with local paths.

Before sending logs or diagnostics, redact usernames, absolute paths, media
names, project names, and filesystem identifiers. Replace enough context to
preserve the failing behavior without disclosing the original asset.

## Immediate containment

Stop processing the affected artifact and keep the original FCPXML unchanged.
Leave `FCP_MCP_ENABLE_LIVE_CONTROL` unset (or set it to `0`) to disable live
Final Cut Pro and Compressor actions. Restrict inputs to a dedicated directory
with `FCP_MCP_ALLOWED_ROOTS`, and do not approve or commit a candidate hash you
have not reviewed. If a workflow reports recovery-required or a ledger
integrity problem, preserve its evidence and use the documented reconciliation
path rather than retrying blindly.

## Security boundaries

- XML parsing uses `defusedxml` wrappers to reject dangerous XML constructs;
  XML input is still untrusted and should remain inside an allowed root.
- The path policy resolves inputs and symlinks and rejects paths outside
  `FCP_MCP_ALLOWED_ROOTS`; outputs cannot replace their source file.
- Live control is disabled by default and requires the explicit
  `FCP_MCP_ENABLE_LIVE_CONTROL=1` opt-in plus macOS Accessibility permission.
- Workflow candidates, semantic diffs, approvals, receipts, and recovery state
  are held in a private local ledger. The ledger is bounded by configured
  operation, source, artifact, and diff limits (defaults: 100 operations,
  128 MiB source, 256 MiB artifacts, and 200 KiB rendered diff).

These controls reduce exposure; they do not make unknown media, imported XML,
or Final Cut Pro’s own scripting surface safe to trust automatically.
