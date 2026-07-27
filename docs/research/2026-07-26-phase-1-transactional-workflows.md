# Phase 1 Research: Transactional Workflows

**Date:** 2026-07-26
**Applies to:** `fcp-mcp` v0.3.0 design
**Repository snapshot:** `0e41c29c5c94605bb39c0b07b2ce18ff6534f3fb`
**Research status:** Complete for design; mandatory revalidation before the
MCP SDK v2 migration and before release

## Question

What is the smallest production-quality graph and loop architecture that can
make multi-step FCPXML changes inspectable, approvable, crash-recoverable, and
machine-verifiable without turning this local MCP server into a generic agent
framework?

## Primary sources reviewed

### MCP protocol and Python SDK

1. [MCP Python SDK on PyPI](https://pypi.org/project/mcp/)
   - `1.28.1` is the current stable release.
   - `2.0.0b2` is the current prerelease.
2. [MCP Python SDK v2.0.0b2 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0b2)
   - The release remains opt-in and targets stable v2 for 2026-07-28.
   - The Tasks extension is still under review and is not implemented.
3. [MCP Python SDK v2 overview](https://py.sdk.modelcontextprotocol.io/v2/whats-new/)
   - `FastMCP` becomes `MCPServer`.
   - The high-level decorator model remains, but imports, context handling,
     transport configuration, result types, and low-level APIs change.
4. [MCP Python SDK v1-to-v2 migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)
   - `mcp.types` moves to the separately packaged `mcp_types` surface.
   - Python attributes change from camel case to snake case while the wire
     representation remains camel case.
   - Server identity can use the public `version=` constructor argument.
   - High-level direct calls return `CallToolResult`.
   - The v2 SDK does not yet implement the redesigned Tasks extension.
5. [MCP Python SDK structured output](https://py.sdk.modelcontextprotocol.io/v2/servers/structured-output/)
   - Tool output schemas derive from return annotations.
   - Returned values are validated before being sent.
   - Results have a text channel for the model and a structured channel for the
     host application.
6. [MCP specification releases](https://github.com/modelcontextprotocol/modelcontextprotocol/releases)
   - `2025-11-25` remains the latest stable revision.
   - `2026-07-28` remains a release candidate and explicitly permits changes
     before final release.
7. [MCP 2026-07-28 release-candidate overview](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
   - Tasks move out of the core protocol into an independently versioned
     extension.
   - The lifecycle is redesigned around stateless requests and negotiated
     extensions.
8. [MCP Tasks extension overview](https://modelcontextprotocol.io/extensions/tasks/overview)
   - Tasks provide durable handles, polling, cancellation, and input-required
     states.
   - Clients and servers must both declare extension support.
   - The extension remains experimental and cannot be assumed across hosts.

### Agent and workflow engineering

1. [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
   - Prefer simple, composable workflows for predictable tasks.
   - Add complexity only when it demonstrates value.
   - Use programmatic gates and environmental ground truth.
2. [Anthropic: Trustworthy agents in practice](https://www.anthropic.com/research/trustworthy-agents)
   - Human control is stronger when a plan can be reviewed before actions run.
   - Tool availability, permissions, harness behavior, and environment all
     contribute to safety.
3. [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
   - A trajectory is the complete sequence of actions and intermediate results.
   - The outcome is the actual environment state, not the agent's success claim.
   - Multiple deterministic graders should be combined where possible.
4. [Temporal workflow execution](https://docs.temporal.io/workflow-execution)
   - Durable workflows use an event history to resume from recorded progress.
   - Replay and explicit execution state provide recovery after failure.
5. [Temporal deterministic workflow constraints](https://docs.temporal.io/workflow-definition#deterministic-constraints)
   - Workflow control flow must be deterministic for replay.
   - External side effects belong outside the replay path and require explicit
     records and retry semantics.

### Local durability

1. [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
   - Explicit transactions are required to make related state transitions
     atomic.
2. [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
   - Rollback journaling provides atomic commit and recovery on local filesystems.
3. [SQLite WAL](https://www.sqlite.org/wal.html)
   - WAL improves concurrency but adds checkpoint and sidecar-file behavior.
   - SQLite documents a WAL-reset corruption bug affecting releases through
     3.51.2, with fixed backports including 3.50.7.
4. [platformdirs on PyPI](https://pypi.org/project/platformdirs/)
   - `4.11.0` is the current production/stable release and supports Python
     3.10 and later.
   - Its `user_state_dir` abstraction covers the supported macOS, Windows, and
     Linux platforms without embedding home-directory conventions in the server.
5. [Hypothesis on PyPI](https://pypi.org/project/hypothesis/)
   - `6.161.5` is the current production/stable release and supports Python
     3.10 and later.
   - It supplies the property-based state-machine and canonicalization coverage
     required by this design without becoming a runtime dependency.

## Local evidence

The current `main` snapshot was inspected before making design decisions.

- The public catalog has 89 tools and five prompts.
- All 89 tools publish an `outputSchema`, but 88 are scalar string wrappers
  shaped like `{"result": "..."}`. Only `fcp_doctor` exposes a meaningful
  domain-object schema.
- Current annotation groups are:
  - 30 diagnostic or offline-read tools;
  - 40 offline mutation or stateful-generation tools;
  - 19 live/open-world tools.
- `src/fcp_mcp/pipeline/` contains only an empty package marker. There is no
  existing workflow engine to preserve.
- `FCPXMLModifier` already applies a useful set of edits in memory before its
  single save boundary. This is the correct initial operation seam.
- The existing transaction layer already validates candidate FCPXML and uses an
  atomic replacement with backup and rollback.
- The active Python 3.12 environment links SQLite `3.50.4`, which is inside the
  WAL-reset bug range documented by SQLite and below its `3.50.7` fixed
  backport.

Two isolated SDK probes were also run without changing project dependencies:

1. Importing the current server under `mcp==2.0.0b2` fails immediately because
   `mcp.server.fastmcp` no longer exists. The migration is not dependency-only.
2. Both `mcp==1.28.1` and `mcp==2.0.0b2` can publish a concrete Pydantic output
   schema while preserving legacy text content by returning a typed
   `CallToolResult`. Meaningful structured outputs do not require waiting for
   v2.

## Findings

### Frozen v0.2.1 public contract

`tests/contracts/v0_2_1_catalog.json` is the checked-in v0.2.1 baseline for
the 89-tool, five-prompt catalog. It captures each tool's name, input schema,
legacy output schema, and annotations. Regenerate it only intentionally with
`scripts/snapshot_tool_contracts.py`; the contract test compares current tool
names, input schemas, annotations, and prompt names against this baseline.

### 1. The correct graph is bounded and code-defined

FCPXML modification has a known critical path:

```text
inspect -> plan -> dry-run -> validate -> diff -> approve -> commit
```

The nodes, transitions, and stopping conditions should be fixed in Python. A
model may propose typed operations, but it must not add graph nodes, select
arbitrary tool calls, skip validation, or choose the approval policy.

This is a deterministic workflow, not an autonomous agent. A general graph
framework would add abstraction and dependency risk without improving the
first use case.

### 2. MCP Tasks cannot be the authoritative run model

The redesigned Tasks extension has the right long-term concepts, but the
Python SDK does not implement it and hosts cannot be assumed to negotiate it.
The server therefore needs its own run IDs, state machine, event history, and
reconciliation logic.

Internal runs may later be adapted to MCP Tasks, but the extension must never
be the only durable record.

### 3. Approval must bind exact evidence

A second model-visible tool call is not proof of human approval. The default
approval path should therefore be an out-of-band CLI command. Its record binds
the run, graph version, source and destination preconditions, canonical plan,
candidate bytes, and diff.

Client-mediated approval remains useful for lower-friction deployments, but it
must be an explicit startup opt-in and its receipts must say that it is weaker.

### 4. Preserve the text channel while fixing the data channel

Changing every successful response to JSON text would impose needless breakage.
The SDK supports a better migration:

- keep the existing human-readable `content`;
- add a concrete, validated `structuredContent`;
- publish that domain model as `outputSchema`.

This supports existing model behavior while giving host applications stable,
machine-readable contracts.

### 5. The ledger is an event history plus projections

A mutable status row alone cannot explain what happened. Each run needs an
append-only event sequence, immutable artifact metadata, approval records, and
a materialized current-state row.

Hash chaining makes accidental or post-hoc modification detectable. It is
tamper-evident, not a claim of adversarial tamper-proof storage.

### 6. Do not use WAL in this release

This is a low-volume local ledger with one writer at a time. Rollback journaling
with `synchronous=FULL` is adequate and avoids relying on a WAL mode affected by
the local runtime's SQLite version.

The design should use:

- rollback journaling;
- `PRAGMA synchronous=FULL`;
- foreign keys;
- a bounded busy timeout;
- `BEGIN IMMEDIATE` for state transitions.

### 7. The SDK boundary should migrate last

Domain models, operation execution, the ledger, approval, and reconciliation do
not need SDK imports. They can be built and verified against stable v1. The
final v2 migration should touch a narrow registration/result boundary after the
stable artifacts and final specification can be inspected.

## Approaches considered

### A. Port to the v2 beta first

Rejected.

- Official guidance says not to use the prerelease in production.
- Its API can still change.
- The target protocol is still an RC.
- Tasks are absent.
- It couples workflow defects to migration defects.

### B. Add a generic graph framework

Rejected for Phase 1.

- The first graph has one fixed critical path.
- Framework state and persistence would compete with the existing atomic-write
  and path-policy boundaries.
- It would make crash reconciliation and evidence harder to inspect.

### C. Add a bounded transactional state machine, then migrate the boundary

Adopted.

- It reuses the current in-memory modifier and transaction seams.
- Deterministic gates provide external evaluation.
- A local event ledger makes approvals and recovery durable.
- Stable v1 can support development without weakening the eventual v2 release
  gate.

## Decisions

### Adopt in v0.3.0

- A fixed FCPXML workflow graph.
- Typed, versioned operation plans and results.
- A durable SQLite event ledger and private hashed artifacts.
- `platformdirs>=4.11,<5` as a direct dependency for the default per-user state
  location.
- `hypothesis>=6.161,<7` as a development-only dependency for property-based
  workflow tests.
- CLI approval by default; explicit client-approval opt-in.
- A default `workflow` capability profile.
- Meaningful typed structured outputs for all existing tools while retaining
  their text responses.
- Outcome and trajectory verification.
- A stable MCP SDK v2 migration only after a fresh release-time research gate.

### Defer

- MCP Tasks integration.
- Generic graph definitions or plugins.
- OpenTelemetry export.
- Knowledge graphs, RAG, model routing, and multi-agent supervision.
- Long-running background workers.
- Template placeholder replacement and clip-identity semantics.
- Live Final Cut Pro actions inside the transactional FCPXML graph.

### Reject

- Shipping a beta SDK as the production dependency.
- Treating tool annotations as approval enforcement.
- Letting the model select approval mode.
- Replanning silently after approval.
- Inferring success after an ambiguous crash.
- A mutable status table without an event history.
- WAL on the current supported runtime matrix.

## Revalidation gate

Immediately before the v2 migration and again before any authorized release:

1. Check PyPI and official GitHub releases for the actual stable SDK.
2. Check the final protocol release and changelog.
3. Re-run the isolated migration and dual-channel output probes.
4. Check whether Tasks shipped and explicitly retain or revise the deferral.
5. Check SQLite versions across the Python 3.10-3.13 test matrix.
6. Record any changed decision in this research document before implementation
   or publication proceeds.
