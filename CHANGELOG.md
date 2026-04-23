# Changelog

All notable changes to fcp-mcp are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/).

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
  XXE/billion-laughs/external-entity blocked, size + depth limits
- Structural validator (`fcpxml/validator.py`) — runs before any
  destructive write
- Lossless FCPXML round-trip (parser + writer preserve effects, roles,
  audio lanes, keyframes, markers, keywords)
- 107 unit tests covering parser, writer, time utilities, analysis,
  generator, puppet system
