# fcp-mcp Documentation

This index separates the current operating contract from dated design and
review evidence.

## Current Operator Documentation

- [README](../README.md) — installation, profiles, capabilities, and safety model
- [LLM guide](../LLM_GUIDE.md) — tool-selection and approval rules for AI clients
- [Workflow recipes](../WORKFLOWS.md) — bounded prepare/review/commit sequences
- [Examples gallery](../examples/GALLERY.md) — representative prompts and calls
- [Troubleshooting](../README.md#troubleshooting) — runtime and policy failures

## Project and Release Documentation

- [Changelog](../CHANGELOG.md) — release-level behavior changes
- [Roadmap](../ROADMAP.md) — planned work
- [Contributing](../CONTRIBUTING.md) — development and verification process
- [v0.3 local release review](reviews/v0.3.0-release-review.md) — exact gates,
  artifact hashes, residual risks, and publication recommendation

## v0.3 Status

The `codex/v0.3-transactional-workflows` branch is a reviewed release
candidate. It is not yet a tagged GitHub release and has not been published to
PyPI or the MCP Registry.

The release changes the default `workflow` profile to a transactional editing
surface. An AI client prepares a private candidate, presents the semantic diff
and candidate hash for review, and commits only the exact approved candidate.
The server persists evidence and supports explicit cancellation and
reconciliation, but does not claim that a chat approval was independently
human-verified.

## Research and Decision Record

Research files are dated inputs to design, not the current product contract.
When a research recommendation differs from current operator documentation,
the README, LLM guide, workflow recipes, and shipped tests define the current
behavior.

- [Trust-boundary research](research/2026-07-26-phase-0-trust-boundary.md)
- [Transactional workflow research](research/2026-07-26-phase-1-transactional-workflows.md)
- [Release-candidate research](research/2026-07-28-v0.3-release-candidate.md)

Implementation plans and specifications live in
[`docs/superpowers/`](superpowers/). They preserve the reasoning and task
history behind the release; they are not a second operator manual.
