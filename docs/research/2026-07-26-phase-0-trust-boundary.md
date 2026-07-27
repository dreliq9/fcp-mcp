# Phase 0 Research: Trustworthy MCP Execution Boundary

**Date:** 2026-07-26
**Applies to:** `fcp-mcp` v0.2.1
**Research gate:** Complete
**Next revalidation:** Before implementation starts, and again before release

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
