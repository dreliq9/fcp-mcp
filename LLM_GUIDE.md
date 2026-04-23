# fcp-mcp LLM Guide

MCP server giving you (Claude) professional-grade Final Cut Pro editing
via FCPXML + AppleScript + ffprobe. 88 tools across 12 categories.
Modified FCPXMLs are written alongside their input with a `_modified`
suffix unless `output_path` is specified.

## Critical: order of operations

Most workflows benefit from inspect → plan → edit → re-verify. Reversing
edit and inspect is the #1 cause of wasted tool calls.

1. **Inspect first.** Run `fcpxml_timeline_stats` and `fcpxml_list_clips`
   before any edit. Without this you will guess clip names wrong.
2. **QC before heal.** Run `fcpxml_qc_report` (or specific `fcpxml_check_*`
   tools) before you start fixing things — you want the full catalog of
   issues, not a whack-a-mole session.
3. **Heal, then re-QC.** After `fcpxml_fix_flash_frames` or
   `fcpxml_fill_gaps`, run QC again to confirm the fix applied.
4. **Batch when you can.** `fcpxml_batch_assign_roles` on 30 clips is one
   call; looping `fcpxml_assign_role` is 30 calls and 30× more context.
5. **Live tools are last.** Do all FCPXML work off-line, then
   `fcp_import_xml` to load it into a running FCP, then trigger Share.

## Resolve clip names by listing first

`fcpxml_list_clips(path)` returns every clip's name, lane, in/out, and
duration. Never guess a clip name — the human may have renamed clips,
used emoji, or left the FCP defaults ("Clip 1", "Clip 1 (duplicate)").
Always list, then copy the name exactly.

## Time values: be explicit

FCPXML uses rational time (`"720/24s"` = 30 seconds at 24fps). Tools
accept the rational form or a timecode string (`"00:00:30:00"`). Prefer
timecode strings in tool calls — they survive frame-rate changes better.
Don't pass raw floats like `30.0` — they may silently quantize wrong.

## QC report anatomy

`fcpxml_qc_report(path)` bundles six checks into one call. Returns a
structured report with:

- **flash_frames** — clips under 2 frames on any visible lane
- **gaps** — silent/black gaps between timeline clips
- **duplicates** — repeated clip names that may indicate accidental copy
- **media_offline** — missing source files (path no longer resolves)
- **frame_rate_conflicts** — clips at a rate different from the sequence
- **audio_level_warnings** — clips peaking above -3 dBFS or below -40
- **safe_zone_violations** — titles/graphics outside 90% action-safe

The tool returns JSON; parse it and decide which `fcpxml_fix_*` or
`fcpxml_check_*` tool to call next.

## Healing vs editing

`heal` tools are **non-destructive reshuffles** of the timeline:
- `fcpxml_fix_flash_frames` — merges <2-frame clips into their neighbor
- `fcpxml_fill_gaps` — extends the preceding clip to close gaps
- `fcpxml_remove_silence` — cuts audio-silent ranges from an interview

`edit` tools **change media or metadata**. If the user says "clean up
this edit," start with heal. If they say "tighten the pacing," use
`edit` (trim, reorder, speed).

## Cross-NLE export

Three targets for the same timeline:

- `fcpxml_export_edl` — flat EDL (CMX3600), lossy but universal
- `fcpxml_export_resolve` — DaVinci Resolve-flavored XML (v1.9). Preserves
  effects, roles (as tracks), audio levels, color labels
- `fcpxml_export_fcp7` — Premiere Pro-compatible XMEML (FCP7 format).
  Roles become tracks; some effects become placeholders

When a user says "send this to color," pick EDL (grade-only) or
`_resolve` (round-trip with VFX). When they say "send to Premiere,"
pick `_fcp7`.

## Live FCP: when to use it, when not

`fcp_*` tools require Final Cut Pro to be **running**. Check first with
`fcp_is_running()`. If false, either ask the user to launch FCP or stay
in FCPXML mode.

Use live tools for:
- Current project/library state (`fcp_get_timeline_info`)
- Triggering Share destinations (`fcp_share("YouTube — 4K")`)
- Dispatching menu commands that have no FCPXML equivalent
  (Organize, Optimize Media, Synchronize Clips)

Don't use live tools for:
- Bulk editing — FCPXML is faster, more reliable, and won't leave FCP in
  a half-edited state
- Queries that work offline — prefer `fcpxml_list_clips` over
  scraping the UI

## Puppet system

`puppet_*` tools emit FCPXML that renders a parametric character
directly in the timeline. No external Motion templates or third-party
plugins — just standards-compliant FCPXML.

```
puppet_create_humanoid_rig(name="walker")
puppet_preset_motion(name="walker", preset="walk", duration="5s")
puppet_multi_scene(
    rigs=[
        {"name": "a", "preset": "walk", "offset": 0},
        {"name": "b", "preset": "talk", "offset": 1},
        {"name": "c", "preset": "wave", "offset": 2},
    ],
    duration="10s",
)
puppet_build_scene(name="walker", output_path="scene.fcpxml")
```

Presets: `walk`, `talk`, `wave`. Each takes a parameter dict
(stride, cycles, arm_swing, etc.) — call `puppet_list_presets()` for the
full parameter catalog. For custom bone/keyframe control bypass the
presets and use `puppet_create_rig` + `puppet_animate` directly.

## Media analysis (ffprobe + ffmpeg)

`media_*` tools require **FFmpeg on $PATH**. If FFmpeg is missing, every
`media_*` call returns an error with install instructions — the tool
doesn't crash, it degrades gracefully.

- `media_info(path)` — streams, duration, codec, sample rate, channel layout
- `media_list_streams(path)` — detailed per-stream metadata
- `media_loudness(path)` — EBU R128 integrated LUFS, LRA, true peak
- `media_detect_silence(path)` — silent ranges (feeds `fcpxml_remove_silence`)
- `media_detect_beats(path)` — musical beat timecodes (feeds beat-sync cuts)
- `media_scene_detect(path)` — scene-change timecodes via ffmpeg scdet
- `media_extract_thumbnail(path, time)` — single JPG at a timecode
- `media_extract_thumbnails(path, count)` — evenly-spaced contact sheet
- `media_extract_audio(path)` — AAC/WAV extraction to disk
- `media_audio_to_midi(path)` — transcribe audio to a MIDI sketch (useful
  for music-driven edits + beat placement)

## Compressor

`compressor_encode(input, setting)` dispatches an Apple Compressor job
asynchronously. The tool returns immediately with a job ID; the encode
runs in the background. Use `compressor_list_settings()` to see what's
installed on the user's machine — Compressor setting names are
user-specific and depend on what presets the user has saved.

## Output directory

Override the default with `FCP_MCP_OUTPUT_DIR` in the MCP env block:

```json
{
  "mcpServers": {
    "fcp": {
      "command": "fcp-mcp",
      "env": { "FCP_MCP_OUTPUT_DIR": "/Users/you/fcp-exports" }
    }
  }
}
```

## Failure modes and recovery

- **"FCPXML schema error"** — input isn't 1.10+. Run `fcpxml_validate`
  first for the specific error. Common cause: user exported from FCP 10.5
  or earlier.
- **"Clip name not found"** — you didn't `fcpxml_list_clips` first.
- **"ffmpeg not found"** — prompt user to `brew install ffmpeg`.
- **"Final Cut Pro is not running"** — either ask the user to launch
  FCP, or switch to FCPXML-only tools.
- **"Accessibility permission denied"** — some `fcp_*` queries need
  System Settings → Privacy & Security → Accessibility access for the
  terminal/Claude binary.

## Escape hatches

None — intentionally. If a tool doesn't exist, file an issue. Don't
ask the user to run a shell command to work around a missing tool;
that's brittle and bypasses validation.
