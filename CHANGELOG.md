# Changelog

All notable changes to fcp-mcp are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/).

## v0.3.0 — 2026-07-28 — Transactional AI piloting

### Added

- A default `workflow` profile with 34 tools, three prompts, and three
  read-only durable evidence resources.
- A fixed prepare, review, hash-bound client approval, atomic commit, cancel,
  status, and explicit recovery flow for bounded FCPXML operations.
- Private SQLite workflow state with tamper-evident events, candidate and diff
  hashes, receipts, destination locks, idempotency, and bounded diagnostics.
- Installed-wheel evidence covering all four profiles and a real
  two-operation prepare/commit/reconcile trajectory.

### Changed

- The full profile now contains 93 tools, five prompts, and three resource
  templates; `inspect`, `edit`, and `full` remain explicit alternatives.
- The MCP boundary now uses the stable Python SDK v2 line and reports the
  fcp-mcp package version as its wire server version.
- Client commits must repeat the exact candidate SHA-256 returned by prepare.
  This proves candidate identity but is recorded as unverified-human approval.
- Python 3.10 compatibility is preserved through `typing_extensions.Self`.
- Commit timestamps retain subsecond precision so restart validation preserves
  event ordering.

### Boundaries

- No candidate XML resource, MCP Tasks, background autonomy, or generic graph
  engine is introduced.
- Selecting `full` does not bypass the independent live-control opt-in.
- `fcpxml_apply_template` remains `unsupported_contract`.

## v0.2.1 — 2026-07-26 — Trust baseline

This patch preserves the existing public tool names and successful text
responses while making the local execution boundary explicit and testable.

### Added

- `fcp_doctor`, bringing the catalog to 89 tools across 12 categories,
  plus the existing five prompts.
- Functional `fcp-mcp --version`, `serve`, `doctor`, and
  `doctor --json` CLI modes.
- Stable coded MCP failures, structured diagnostics, tool annotations,
  path scoping, transactional FCPXML receipts, and machine-validated
  documentation examples.
- Configuration for allowed input roots, opt-in live control, and text
  or JSON transaction events.

### Changed

- Runtime SDK dependency is bounded to `mcp>=1.27,<2`.
- FCPXML writes use validated same-directory temporary files, backups,
  atomic replacement, and post-commit verification.
- AppleScript and JXA dynamic values are delivered through `run argv`
  instead of being interpolated into program source.
- Media and Compressor calls verify command status and promised output.
- Live FCP and Compressor actions are disabled until
  `FCP_MCP_ENABLE_LIVE_CONTROL` is explicitly enabled.
- All 89 tools carry public MCP safety annotations.
- The tag workflow isolates verification from publication and uses PyPI
  Trusted Publishing instead of a stored API token.

### Corrected contracts

- Known domain failures now return MCP `isError: true` with stable codes.
- `fcpxml_apply_template` returns `unsupported_contract`; v0.2.1 has no
  stable clip-substitution schema and does not pretend to apply one.
- QC reports are documented as Markdown covering validation, statistics,
  gaps, flash frames, duplicate sources, and pacing.
- Beat-driven rough cuts are documented as cadence approximations, not
  frame-exact beat placement.

### FastMCP v1 identity disclosure

FastMCP v1 has no public constructor parameter for an application
version. The initialize response therefore reports the installed MCP SDK
version in `serverInfo.version`. The CLI, server instructions, and doctor
report the `fcp-mcp` package version separately. A public wire-identity
fix is deferred to the stable MCP Python SDK v2 migration.

## v0.2.0 — 2026-04-23 — MCP Prompts

Adds five `@mcp.prompt()` decorators that wrap the most common tool
sequences so MCP clients can invoke them by name instead of chaining
tool calls by hand.

### Prompts (5)

- **`qc-check`** — runs `fcpxml_qc_report` and interprets the results as a
  triaged checklist (auto-fixable vs. manual).
- **`rough-cut`** — wraps `fcpxml_auto_rough_cut` with an optional target
  duration, then re-QCs the output.
- **`cleanup`** — chains `fcpxml_fix_flash_frames` → `fcpxml_fill_gaps` →
  `fcpxml_qc_report` and reports before/after counts.
- **`youtube-chapters`** — extracts FCPXML markers and emits a
  YouTube-ready chapter blob (`HH:MM:SS Title`).
- **`beat-sync`** — calls `media_detect_beats` then constrains rough-cut
  clip length using the detected cadence. Exact beat cut points are not
  part of the v0.2 tool schema.

No breaking changes. All 88 tools from v0.1.0 are unchanged.

## v0.1.0 — 2026-04-22 — Initial public release

First public release. 88 tools across 12 categories covering FCPXML
editing, live FCP control, parametric puppets, media analysis, and
Compressor dispatch — the first MCP server to cover all four layers.

### Tools (88)

- **inspect (8)** — `fcpxml_parse`, `fcpxml_list_clips`, `fcpxml_list_markers`,
  `fcpxml_list_effects`, `fcpxml_list_roles`, `fcpxml_analyze_pacing`,
  `fcpxml_timeline_stats`, `fcpxml_diff`
- **qc (10)** — `fcpxml_detect_gaps`, `fcpxml_detect_flash_frames`,
  `fcpxml_detect_duplicates`, `fcpxml_validate`, `fcpxml_qc_report`,
  `fcpxml_check_media_links`, `fcpxml_check_frame_rates`,
  `fcpxml_check_audio_levels`, `fcpxml_check_safe_zones`,
  `fcpxml_check_duration`
- **edit (12)** — `fcpxml_add_marker`, `fcpxml_add_keyword`,
  `fcpxml_add_title`, `fcpxml_add_audio`, `fcpxml_add_transition`,
  `fcpxml_trim_clip`, `fcpxml_split_clip`, `fcpxml_delete_clips`,
  `fcpxml_reorder_clips`, `fcpxml_change_speed`, `fcpxml_assign_role`,
  `fcpxml_reformat`
- **heal (3)** — `fcpxml_fix_flash_frames`, `fcpxml_fill_gaps`,
  `fcpxml_remove_silence`
- **batch (4)** — `fcpxml_batch_add_markers`, `fcpxml_batch_rename_clips`,
  `fcpxml_batch_assign_roles`, `fcpxml_batch_apply_transition`
- **generate (4)** — `fcpxml_create_project`, `fcpxml_create_timeline`,
  `fcpxml_auto_rough_cut`, `fcpxml_generate_montage`
- **templates (3)** — `fcpxml_list_templates`, `fcpxml_apply_template`,
  `fcpxml_save_template`
- **io (5)** — `fcpxml_import_srt`, `fcpxml_import_edl`,
  `fcpxml_export_edl`, `fcpxml_export_resolve`, `fcpxml_export_fcp7`
- **live (20)** — `fcp_is_running`, `fcp_get_app_state`, `fcp_get_libraries`,
  `fcp_get_events`, `fcp_get_projects`, `fcp_get_timeline_info`,
  `fcp_open_library`, `fcp_import_xml`, `fcp_export_xml`,
  `fcp_playback`, `fcp_navigate`, `fcp_select_tool`, `fcp_undo`, `fcp_redo`,
  `fcp_menu_command`, `fcp_keyboard_shortcut`, `fcp_share`,
  `fcp_discover_effects`, `fcp_list_motion_templates`,
  `fcp_list_share_destinations`
- **puppet (7)** — `puppet_create_rig`, `puppet_create_humanoid_rig`,
  `puppet_build_scene`, `puppet_animate`, `puppet_preset_motion`,
  `puppet_multi_scene`, `puppet_list_presets`
- **media (10)** — `media_info`, `media_list_streams`, `media_loudness`,
  `media_detect_silence`, `media_detect_beats`, `media_scene_detect`,
  `media_extract_thumbnail`, `media_extract_thumbnails`,
  `media_extract_audio`, `media_audio_to_midi`
- **compressor (2)** — `compressor_list_settings`, `compressor_encode`

### Foundations

- Rational-arithmetic timecode (`time_utils.py`) — frame-accurate across
  23.976 / 24 / 29.97 / 59.94 / drop-frame without rounding drift
- Hardened XML parsing (`utils/safe_xml.py`) — `defusedxml`-backed,
  XXE/billion-laughs/external-entity blocked
- Structural validator (`fcpxml/validator.py`) — checks the implemented
  FCPXML invariants; it is not a complete Apple schema validator
- Tree-preserving FCPXML mutation retains supported effects, roles,
  audio lanes, keyframes, markers, and keywords
- 107 unit tests covering parser, writer, time utilities, analysis,
  generator, puppet system
