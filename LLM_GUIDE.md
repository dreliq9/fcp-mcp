# fcp-mcp LLM Guide

MCP server giving you (Claude) professional-grade Final Cut Pro editing
via FCPXML + AppleScript + ffprobe. The default `workflow` profile exposes
34 tools, three prompts, and three workflow resources; `full` exposes 93
tools across 13 categories and five prompts.
Modified FCPXMLs are written alongside their input with a `_modified`
suffix unless `output_path` is specified.

## Default transactional contract

In the default profile, do not call a direct mutator. Call
`fcpxml_workflow_prepare` with an explicit source, destination, and bounded
operation list. Prepare changes only private workflow evidence. Show the
returned summary, warnings, `diff_uri`, and `candidate_sha256` to the user.
After explicit approval of that exact candidate, call
`fcpxml_workflow_commit` with the same run ID and
`expected_candidate_sha256` set to the exact hash returned by prepare.

Never infer approval, omit the hash, or commit a different candidate. Client
approval is recorded as `client_unverified_human`: the hash binding is
verified, but the server cannot verify who approved the surrounding chat.

Read durable evidence from `fcp-workflow://runs/{run_id}`,
`fcp-workflow://runs/{run_id}/events`, and
`fcp-workflow://runs/{run_id}/diff`. There is intentionally no candidate XML
resource. Use `fcp-mcp workflow reconcile RUN_ID --json` only for explicit
recovery assessment; no background agent reconciles runs.

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

```tool-call
{"name":"fcpxml_list_clips","arguments":{"path":"show.fcpxml"}}
```

## Time values: be explicit

FCPXML uses rational time (`"720/24s"` = 30 seconds at 24fps). Tools
accept the rational form or a timecode string (`"00:00:30:00"`). Prefer
timecode strings in tool calls — they survive frame-rate changes better.
Don't pass raw floats like `30.0` — they may silently quantize wrong.

## QC report anatomy

`fcpxml_qc_report` returns Markdown:

```tool-call
{"name":"fcpxml_qc_report","arguments":{"path":"show.fcpxml"}}
```

Its sections cover structural validation, timeline statistics, gaps, flash
frames, duplicate sources, and pacing. Media links, frame rates, audio
levels, safe zones, and target duration are separate tools; do not claim
that the aggregate report ran them.

```tool-call
{"name":"fcpxml_check_media_links","arguments":{"path":"show.fcpxml"}}
```

```tool-call
{"name":"fcpxml_check_frame_rates","arguments":{"path":"show.fcpxml"}}
```

```tool-call
{"name":"fcpxml_check_audio_levels","arguments":{"path":"show.fcpxml"}}
```

```tool-call
{"name":"fcpxml_check_safe_zones","arguments":{"path":"show.fcpxml"}}
```

## Healing vs editing

The three cleanup tools mutate timeline structure and must be reviewed:

- `fcpxml_fix_flash_frames` extends clips shorter than a configured
  minimum; it does not merge them into a neighbor.

```tool-call
{"name":"fcpxml_fix_flash_frames","arguments":{"path":"show.fcpxml","output_path":"show_clean.fcpxml"}}
```

- `fcpxml_fill_gaps` replaces gap elements with clips referencing an
  existing asset ID.

```tool-call
{"name":"fcpxml_fill_gaps","arguments":{"path":"show_clean.fcpxml","fill_asset_ref":"r2","output_path":"show_clean.fcpxml"}}
```

- `fcpxml_remove_silence` removes FCPXML gap elements at or above the
  threshold. It does not consume FFmpeg silence ranges or splice silence
  out of source clips.

```tool-call
{"name":"fcpxml_remove_silence","arguments":{"path":"show_clean.fcpxml","silence_threshold_seconds":2.0,"output_path":"show_clean.fcpxml"}}
```

After any cleanup, inspect the output and re-run the relevant checks.

## Cross-NLE export

Three conversion targets are available. Treat every conversion as lossy
and inspect the output in the destination NLE:

```tool-call
{"name":"fcpxml_export_edl","arguments":{"path":"show.fcpxml","output_path":"show.edl"}}
```

```tool-call
{"name":"fcpxml_export_resolve","arguments":{"path":"show.fcpxml","output_path":"show_resolve.xml"}}
```

```tool-call
{"name":"fcpxml_export_fcp7","arguments":{"path":"show.fcpxml","output_path":"show_fcp7.xml"}}
```

When a user says "send this to color," pick EDL (grade-only) or
`_resolve` (round-trip with VFX). When they say "send to Premiere,"
pick `_fcp7`.

## Live FCP: when to use it, when not

`fcp_*` tools require Final Cut Pro to be **running**. Check first:

```tool-call
{"name":"fcp_is_running","arguments":{}}
```

If false, either ask the user to launch FCP or stay in FCPXML mode.

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

Validate a conventional image directory:

```tool-call
{"name":"puppet_create_humanoid_rig","arguments":{"name":"walker","image_dir":"characters/walker"}}
```

Inspect supported presets:

```tool-call
{"name":"puppet_list_presets","arguments":{}}
```

Puppet calls do not retain a rig by name between invocations. Pass the
complete rig JSON string to preset motion:

```tool-call
{"name":"puppet_preset_motion","arguments":{"rig_json":"{\"name\":\"walker\",\"parts\":[{\"name\":\"body\",\"image\":\"characters/walker/body.png\"}]}","preset":"walk","duration":"5s","output_path":"walker.fcpxml"}}
```

Multi-scene generation likewise takes serialized rig and scene arrays:

```tool-call
{"name":"puppet_multi_scene","arguments":{"rigs_json":"[{\"name\":\"walker\",\"parts\":[{\"name\":\"body\",\"image\":\"characters/walker/body.png\"}]}]","scenes_json":"[{\"name\":\"intro\",\"duration\":\"5s\",\"preset\":\"idle\"},{\"name\":\"walk\",\"duration\":\"5s\",\"preset\":\"walk\"}]","project_name":"Walker","output_path":"scenes"}}
```

For custom keyframe control, provide serialized rigs and animations to
`puppet_animate`.

## Media analysis (ffprobe + ffmpeg)

`media_*` tools require **FFmpeg on $PATH**. If FFmpeg is missing, the
tool returns a coded `dependency_missing` MCP error.

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

For example:

```tool-call
{"name":"media_detect_silence","arguments":{"path":"interview.mov","noise_threshold":"-40dB","min_duration":0.8}}
```

```tool-call
{"name":"media_extract_thumbnail","arguments":{"path":"interview.mov","time":30.0,"output_path":"thumb.jpg"}}
```

## Compressor

List the local settings first:

```tool-call
{"name":"compressor_list_settings","arguments":{}}
```

Then pass a concrete `.cmprstng` path:

```tool-call
{"name":"compressor_encode","arguments":{"input_path":"hero.mov","setting_path":"Presets/YouTube 4K.cmprstng","output_dir":"deliverables","batch_name":"YouTube 4K"}}
```

The tool invokes Compressor's CLI and returns its captured submission
output. The current tool does not expose a normalized job ID or completion
tracking.

## Runtime boundaries

Set the canonical directory for relative inputs and generated outputs
with `FCP_MCP_OUTPUT_DIR`. Legacy `FCP_PROJECTS_DIR` is consulted only
when the canonical variable is unset.

```json
{
  "mcpServers": {
    "fcp": {
      "command": "fcp-mcp",
      "env": {
        "FCP_MCP_OUTPUT_DIR": "/Users/you/fcp-exports",
        "FCP_MCP_ALLOWED_ROOTS": "/Users/you/Movies:/Volumes/Media",
        "FCP_MCP_ENABLE_LIVE_CONTROL": "0",
        "FCP_MCP_LOG_FORMAT": "json"
      }
    }
  }
}
```

`FCP_MCP_PROFILE` selects `inspect`, `workflow` (default), `edit`, or `full`.
`edit` and `full` expose direct mutators; `full` still does not enable live
control. `FCP_MCP_STATE_DIR` contains the private workflow ledger and
artifacts.

`FCP_MCP_ALLOWED_ROOTS` uses the platform path separator. Inputs are
resolved after symlinks and must remain beneath one of these roots;
external volumes are not implicitly available. Live FCP, Accessibility,
and Compressor actions require an explicit true value for
`FCP_MCP_ENABLE_LIVE_CONTROL`.

Never ask an edit tool to write back to its source path: fcp-mcp returns
`same_file_forbidden`. Existing destinations receive a sibling backup
before atomic replacement. The built-in validator proves safe parsing
and its implemented structural invariants, not complete Apple/FCP
compatibility; use a disposable Final Cut Pro import as the
authoritative gate for an important result.

## Failure modes and recovery

- **`validation_failed`** — run `fcpxml_validate` for the implemented
  structural checks. Treat a disposable Final Cut Pro import as the
  authoritative compatibility test.
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
