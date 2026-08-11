# YouTube-MCP clip-plan handoff for v0.3.0

FCP-MCP can consume the editor-neutral materialized clip manifest produced by `dreliq9/youtube-mcp-v2` without teaching youtube-mcp about Final Cut Pro.

## Boundary

```text
youtube-mcp
  corpus.clip_plan
      ↓
  media.materialize
      ↓
  youtube-mcp.materialized-clip-plan/v1
      ↓
fcp-mcp
  fcpxml_generate_from_clip_plan
      ↓
  native FCPXML + provenance sidecar
      ↓
  normal FCP-MCP inspect/QC/workflow/live-import operations
```

YouTube-MCP owns discovery, evidence selection, source timestamps, source acquisition, and materialized clip hashes. FCP-MCP owns local path policy, integrity revalidation, FCPXML generation, subsequent editing, QC, and optional Final Cut Pro import/control.

## Tool

`fcpxml_generate_from_clip_plan` is an `OFFLINE_WRITE` tool. In v0.3.0 it
exists only in the `edit` and `full` profiles—not in the default review-only
`workflow` profile or the `inspect` profile.

```text
fcpxml_generate_from_clip_plan(
  manifest_path,
  project_name="YouTube Remix",
  output_path=""
)
```

Hash verification is mandatory. The former `verify_hashes` input was removed
before the first public release; no published caller relied on the opt-out.

The tool:

1. resolves the manifest through the normal FCP-MCP input path policy,
2. requires schema `youtube-mcp.materialized-clip-plan/v1`,
3. resolves every local media path through the same path policy,
4. requires a valid SHA-256 digest and verifies every materialized asset's content,
5. preserves manifest ordering,
6. uses the existing `FCPXMLGenerator` to create a native primary-storyline timeline,
7. commits the FCPXML through the existing atomic generator transaction path,
8. writes `<project>.sources.json` with the original YouTube URLs, source time ranges, clip hashes, plan revision, and materialization revision.

Materialized youtube-mcp assets have already been source-trimmed. FCP-MCP therefore uses each local clip from `0s` for its full `duration_s`; it does not reinterpret the original YouTube in/out points.

## Path policy

FCP-MCP deliberately does not bypass its allowed-root policy for youtube-mcp assets. If youtube-mcp materializes clips under its default cache, that cache root must be readable under the configured FCP-MCP input roots.

Typical local configuration can include the youtube-mcp cache root in `FCP_MCP_ALLOWED_ROOTS` along with the user's normal media roots.

This is intentional: provenance and SHA validation establish media identity, but they do not grant filesystem authority.

## Recommended workflow

1. use youtube-mcp to compose/freeze the source corpus,
2. retrieve evidence and create a `corpus.clip_plan`,
3. review/reorder/refine the planned source moments if desired,
4. call youtube-mcp `media.materialize`,
5. call FCP-MCP `fcpxml_generate_from_clip_plan` under the `edit` profile,
6. inspect the generated FCPXML and run pacing/QC checks,
7. use the durable prepare/review/commit workflow for subsequent edits when appropriate,
8. optionally import into a disposable Final Cut Pro project and continue with live control under the separately enabled `full` profile.

## Provenance

The `.sources.json` sidecar is deliberately separate from FCPXML creative semantics. It preserves the exact research/media lineage without adding visible timeline markers or abusing Final Cut metadata fields.

Publication rights are not inferred by either MCP. The provenance is retained so a human or downstream policy layer can evaluate attribution, licensing, platform policy, and fair-use/fair-dealing context before distribution.
