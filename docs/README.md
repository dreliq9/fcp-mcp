# fcp-mcp Documentation

This index separates the current operating contract from dated design and
review evidence.

## Current Operator Documentation

- [README](../README.md) — installation, profiles, capabilities, and safety model
- [First run](FIRST_RUN.md) — install, verify, and choose the default profile
- [Compatibility](COMPATIBILITY.md) — bounded FCPXML interchange and Final Cut Pro handoff
- [Support](../SUPPORT.md) — maintainer-owned help and issue routing
- [Security](../SECURITY.md) — private vulnerability reporting
- [LLM guide](../LLM_GUIDE.md) — tool-selection and approval rules for AI clients
- [Workflow recipes](../WORKFLOWS.md) — bounded prepare/review/commit sequences
- [YouTube-MCP clip-plan handoff](YOUTUBE_MCP_HANDOFF.md) — generate native FCPXML from provenance-preserving youtube-mcp materialized clip plans
- [Examples gallery](../examples/GALLERY.md) — representative prompts and calls
- [Troubleshooting](../README.md#troubleshooting) — runtime and policy failures

## Project and Release Documentation

- [Changelog](../CHANGELOG.md) — release-level behavior changes
- [Roadmap](../ROADMAP.md) — planned work
- [Contributing](../CONTRIBUTING.md) — development and verification process
- [v0.3.0 release notes](releases/v0.3.0.md) — release identity and known boundaries
- [v0.3.0 review evidence](reviews/v0.3.0-release-review.md) — exact gates,
  artifact hashes, tested Final Cut Pro handoff evidence, and residual risks

## v0.3.0 product contract

Version 0.3.0 provides a 94-tool catalog: `inspect=30`, `workflow=34`,
`edit=75`, and `full=94`. The default `workflow` profile is a bounded
transactional FCPXML surface: inspect, prepare, review, hash approval, then
commit. It never directly mutates FCPXML or Final Cut Pro.

The release supports bounded FCPXML interchange and optional Final Cut Pro
handoff. The documented review records DTD and disposable-project canaries;
these are evidence for the tested handoff, not a claim of complete autonomous
Final Cut Pro control. `fcpxml_generate_from_clip_plan` accepts
`youtube-mcp.materialized-clip-plan/v1` provenance manifests in the `edit` and
`full` profiles only.

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
