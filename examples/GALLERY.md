# Scenario Gallery

These scenarios show where the v0.2.1 surface is useful and where its
boundaries are. The machine-validated calls live in
[`WORKFLOWS.md`](../WORKFLOWS.md); this page is the short capability map.

## Structural QC followed by explicit cleanup

Generate the Markdown structural report, decide whether extending short
clips or replacing gap elements is appropriate, make the chosen
mutation, then re-run the report. Media links, frame rates, audio
levels, and safe zones remain separate checks. Gap filling requires an
existing asset resource ID and changes timeline media.

## SRT captions

Import an SRT into an existing FCPXML document, write to a distinct
output, and run the safe-zone or structural checks afterward. v0.2.1
uses the `Titles.Subtitle` role and lane 1; it does not expose a caption
font-preset or arbitrary-lane parameter.

## Beat-informed rough cut

Detect beat timestamps, compute a representative inter-beat cadence,
and use that cadence as `max_clip_duration` for the rough-cut generator.
The result approximates musical pacing. v0.2.1 does not accept
individual cut points and must not be described as frame-exact beat
sync.

## Cross-NLE handoff

Check media links before producing Resolve XML, FCP7 XMEML, or EDL.
Treat each conversion as lossy and inspect it in the destination NLE.
There is no v0.2.1 media-relink tool.

## Authorized live FCP and Compressor flow

Start with `fcp_doctor` and `fcp_is_running`. Live FCP, Accessibility,
and Compressor actions require `FCP_MCP_ENABLE_LIVE_CONTROL=1`; offline
tools remain available while it is disabled. Compressor returns checked
CLI submission output, not normalized job tracking or completion state.

## Puppet FCPXML generation

Pass complete serialized rig data to each generation call—rig names are
not durable cross-call state. Validate the generated FCPXML
structurally, then use a disposable Final Cut Pro project as the
authoritative import/render check.
