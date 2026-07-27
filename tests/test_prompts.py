"""Tests for the v0.2 MCP prompts registered on the server."""

from __future__ import annotations

import asyncio

import pytest

from fcp_mcp.server import (
    mcp,
    prompt_beat_sync,
    prompt_cleanup,
    prompt_qc_check,
    prompt_rough_cut,
    prompt_youtube_chapters,
)
from scripts.check_contracts import extract_tool_call_blocks, validate_call

EXPECTED_PROMPT_NAMES = {
    "qc-check",
    "rough-cut",
    "cleanup",
    "youtube-chapters",
    "beat-sync",
}


def test_all_five_prompts_registered():
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert EXPECTED_PROMPT_NAMES.issubset(names), (
        f"missing prompts: {EXPECTED_PROMPT_NAMES - names}"
    )


def test_cleanup_prompt_requires_fill_asset_ref():
    prompts = {prompt.name: prompt for prompt in asyncio.run(mcp.list_prompts())}
    arguments = {
        argument.name: argument.required
        for argument in prompts["cleanup"].arguments or []
    }

    assert arguments == {
        "path": True,
        "fill_asset_ref": True,
        "output_path": False,
    }


def test_qc_check_mentions_qc_report():
    body = prompt_qc_check(path="/tmp/a.fcpxml")
    assert "fcpxml_qc_report" in body
    assert "/tmp/a.fcpxml" in body


def test_rough_cut_includes_clips_and_target():
    body = prompt_rough_cut(clips_json='[{"src":"a.mov","duration":"30s"}]', target_duration="60s")
    assert "fcpxml_auto_rough_cut" in body
    assert "60s" in body
    assert "a.mov" in body


def test_rough_cut_without_target_uses_all_clips():
    body = prompt_rough_cut(clips_json="[]")
    assert "all clips" in body


def test_cleanup_sequences_fix_fill_qc():
    body = prompt_cleanup(path="/tmp/x.fcpxml", fill_asset_ref="r2")
    # Order matters: fix_flash_frames → fill_gaps → qc_report
    i_fix = body.index("fcpxml_fix_flash_frames")
    i_fill = body.index("fcpxml_fill_gaps")
    i_qc = body.index("fcpxml_qc_report")
    assert i_fix < i_fill < i_qc
    assert "r2" in body


def test_youtube_chapters_forces_zero_start():
    body = prompt_youtube_chapters(path="/tmp/show.fcpxml")
    assert "fcpxml_list_markers" in body
    assert "00:00" in body


def test_beat_sync_chains_detect_and_rough_cut():
    body = prompt_beat_sync(
        audio_path="/tmp/track.wav",
        clips_json='[{"src":"a.mov","duration":"5s"}]',
    )
    assert "media_detect_beats" in body
    assert "fcpxml_auto_rough_cut" in body
    assert "/tmp/track.wav" in body


@pytest.mark.parametrize(
    "prompt_fn,kwargs",
    [
        (prompt_qc_check, {"path": "p"}),
        (prompt_rough_cut, {"clips_json": "[]"}),
        (prompt_cleanup, {"path": "p", "fill_asset_ref": "r2"}),
        (prompt_youtube_chapters, {"path": "p"}),
        (prompt_beat_sync, {"audio_path": "a", "clips_json": "[]"}),
    ],
)
def test_prompts_return_non_empty_strings(prompt_fn, kwargs):
    out = prompt_fn(**kwargs)
    assert isinstance(out, str)
    assert len(out) > 50


@pytest.mark.parametrize(
    "prompt_fn,kwargs",
    [
        (prompt_qc_check, {"path": "show.fcpxml"}),
        (
            prompt_rough_cut,
            {
                "clips_json": '[{"src":"a.mov","duration":"5s"}]',
                "target_duration": "30s",
            },
        ),
        (
            prompt_cleanup,
            {
                "path": "show.fcpxml",
                "fill_asset_ref": "r2",
                "output_path": "show_clean.fcpxml",
            },
        ),
        (prompt_youtube_chapters, {"path": "show.fcpxml"}),
        (
            prompt_beat_sync,
            {
                "audio_path": "track.wav",
                "clips_json": '[{"src":"a.mov","duration":"5s"}]',
            },
        ),
    ],
)
def test_rendered_prompt_calls_match_live_catalog(prompt_fn, kwargs):
    body = prompt_fn(**kwargs)
    blocks = extract_tool_call_blocks(body)
    catalog = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    failures = [
        f"block {block_number}: {error}"
        for block_number, call in blocks
        for error in validate_call(catalog, call)
    ]

    assert blocks
    assert failures == []
