"""Contract tests for the public MCP tool catalog."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from fcp_mcp.server import mcp

EXPECTED_TOOLS = {
    "fcpxml_parse",
    "fcpxml_list_clips",
    "fcpxml_list_markers",
    "fcpxml_analyze_pacing",
    "fcpxml_detect_gaps",
    "fcpxml_detect_flash_frames",
    "fcpxml_detect_duplicates",
    "fcpxml_validate",
    "fcpxml_list_effects",
    "fcpxml_list_roles",
    "fcpxml_timeline_stats",
    "fcpxml_diff",
    "fcpxml_add_marker",
    "fcpxml_batch_add_markers",
    "fcpxml_add_keyword",
    "fcpxml_trim_clip",
    "fcpxml_split_clip",
    "fcpxml_delete_clips",
    "fcpxml_reorder_clips",
    "fcpxml_add_transition",
    "fcpxml_change_speed",
    "fcpxml_assign_role",
    "fcpxml_add_title",
    "fcpxml_add_audio",
    "fcpxml_create_project",
    "fcpxml_create_timeline",
    "fcpxml_auto_rough_cut",
    "fcpxml_generate_montage",
    "fcpxml_import_srt",
    "fcpxml_import_edl",
    "fcpxml_reformat",
    "fcpxml_fix_flash_frames",
    "fcpxml_fill_gaps",
    "fcpxml_remove_silence",
    "fcpxml_batch_rename_clips",
    "fcpxml_batch_assign_roles",
    "fcpxml_batch_apply_transition",
    "fcpxml_qc_report",
    "fcpxml_check_media_links",
    "fcpxml_check_frame_rates",
    "fcpxml_check_audio_levels",
    "fcpxml_check_safe_zones",
    "fcpxml_check_duration",
    "fcp_list_motion_templates",
    "fcp_list_share_destinations",
    "fcp_discover_effects",
    "fcpxml_list_templates",
    "fcpxml_apply_template",
    "fcpxml_save_template",
    "fcp_is_running",
    "fcp_get_libraries",
    "fcp_get_events",
    "fcp_get_projects",
    "fcp_get_timeline_info",
    "fcp_get_app_state",
    "fcp_open_library",
    "fcp_import_xml",
    "fcp_export_xml",
    "fcp_playback",
    "fcp_navigate",
    "fcp_select_tool",
    "fcp_undo",
    "fcp_redo",
    "fcp_menu_command",
    "fcp_keyboard_shortcut",
    "fcp_share",
    "compressor_encode",
    "compressor_list_settings",
    "fcpxml_export_resolve",
    "fcpxml_export_fcp7",
    "fcpxml_export_edl",
    "media_info",
    "media_detect_silence",
    "media_detect_beats",
    "media_loudness",
    "media_extract_thumbnail",
    "media_extract_thumbnails",
    "media_list_streams",
    "media_scene_detect",
    "media_extract_audio",
    "media_audio_to_midi",
    "puppet_create_rig",
    "puppet_create_humanoid_rig",
    "puppet_build_scene",
    "puppet_animate",
    "puppet_preset_motion",
    "puppet_multi_scene",
    "puppet_list_presets",
    "fcp_doctor",
    "fcpxml_workflow_prepare",
    "fcpxml_workflow_status",
    "fcpxml_workflow_commit",
    "fcpxml_workflow_cancel",
}


def test_full_catalog_is_exactly_93_annotated_tools():
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(tools) == 93
    assert all(tool.annotations is not None for tool in tools)


def test_real_no_environment_default_exposes_workflow_safe_catalog():
    environment = os.environ.copy()
    environment.pop("FCP_MCP_PROFILE", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio, json; from fcp_mcp.server import mcp; "
                "print(json.dumps({"
                "'tools': [item.name for item in asyncio.run(mcp.list_tools())], "
                "'prompts': [item.name for item in asyncio.run(mcp.list_prompts())]"
                "}))"
            ),
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    catalog = json.loads(completed.stdout)

    assert len(catalog["tools"]) == 34
    assert catalog["tools"][0] == "fcp_doctor"
    assert catalog["tools"][-1] == "fcpxml_workflow_cancel"
    assert "fcpxml_add_marker" not in catalog["tools"]
    assert "fcp_open_library" not in catalog["tools"]
    assert "fcpxml_workflow_prepare" in catalog["tools"]
    assert catalog["prompts"] == ["qc-check", "cleanup", "youtube-chapters"]
