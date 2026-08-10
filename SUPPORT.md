# Support

For setup, compatibility, and first-run help, open a GitHub issue with the
appropriate form. GitHub Discussions are not enabled as a support surface for
0.3.0. Do not use an issue for a security report; follow [SECURITY.md](SECURITY.md).

## Before filing

Run:

```bash
fcp-mcp --version
fcp-mcp doctor --json
```

Redact private paths, usernames, media names, project names, and filesystem
identifiers before posting output. Never post media, credentials, or an
unredacted FCPXML file.

## Required bug-report details

Include all of the following:

- fcp-mcp version and installation method (`pipx`, wheel, or source checkout)
- MCP client and client version (for example Claude Code or Claude Desktop)
- selected profile (`inspect`, `workflow`, `edit`, or `full`) and whether
  `FCP_MCP_ENABLE_LIVE_CONTROL` was enabled
- macOS version, Final Cut Pro version if relevant, and Python version
- redacted `fcp-mcp doctor --json` output
- a minimal, safe reproduction: expected result, actual result, exact tool or
  CLI command, and the error or receipt fields

For FCPXML problems, say whether `fcpxml_validate`, the Apple DTD check, and a
disposable-library import were attempted. A private sample that reproduces the
problem is preferable to production media.

## Feature requests

State the editing or interchange problem, the smallest useful outcome, why the
current bounded workflow is insufficient, and any compatibility constraints.
Requests for autonomous live Final Cut Pro control, generic native-library
mutation, proxy relinking, or template substitution are outside the 0.3.0
contract unless a future release explicitly adopts them.
