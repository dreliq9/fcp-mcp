# fcp-mcp Workflows

Production recipes using fcp-mcp tools. Each workflow lists the agent
prompt, the tool sequence, and what makes the workflow non-trivial
without fcp-mcp.

For a one-shot verification that your install works, see
[`examples/quickstart.py`](examples/quickstart.py). For a visual gallery
with renders and narratives, see [`examples/GALLERY.md`](examples/GALLERY.md).

The default `workflow` profile supports inspection plus durable
prepare/review/commit editing. Recipes below that name direct mutators require
the `edit` or `full` profile. Prefer the transactional form whenever the
requested change can be expressed by a workflow operation.

---

## 1. Structural QC pass on a locked cut

**When to use:** right before you bounce a master. Catches flash frames,
gaps, media drift, level problems, and safe-zone breaches in one pass.

```
You: "Run the structural QC report on hero.fcpxml and give me the summary."
```

**Tools (one call):**

```tool-call
{"name":"fcpxml_qc_report","arguments":{"path":"hero.fcpxml"}}
```

**What you get back:** a Markdown report containing structural validation,
timeline statistics, gaps, flash frames, duplicate sources, and pacing.
This aggregate tool does not include the separate media-link,
frame-rate, audio-level, or safe-zone checks.

**Why fcp-mcp:** one call produces a deterministic structural summary
directly from the FCPXML document.

---

## 2. Heal pass after QC

**When to use:** follow-up to recipe 1 for explicit timeline mutations
that address flash frames and gap elements.

```
You: "Fix the flash frames and close the gaps, then re-run QC."
```

**Tools:**

1. Prepare both changes without touching `hero_heal.fcpxml`:

```tool-call
{"name":"fcpxml_workflow_prepare","arguments":{"source_path":"hero.fcpxml","destination_path":"hero_heal.fcpxml","operations":[{"kind":"fix_flash_frames","min_frames":3,"frame_duration":"1001/30000s"},{"kind":"fill_gaps","fill_ref":"r2","fill_name":"Fill"}]}}
```

2. Review the returned summary, warnings, hashes, and `diff_uri`. After the
   user explicitly approves that exact candidate, commit it:

```tool-call
{"name":"fcpxml_workflow_commit","arguments":{"run_id":"123e4567-e89b-42d3-a456-426614174000","expected_candidate_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
```

Replace the example run ID and hash with the exact values returned by prepare.
The selected `r2` resource must already exist in the source FCPXML.

3. Re-run the structural report after commit:

```tool-call
{"name":"fcpxml_qc_report","arguments":{"path":"hero_heal.fcpxml"}}
```

**What you get back:** the modified FCPXML plus a post-change Markdown
QC report. A zero-item result is valid; do not describe the file as
clean unless the final report supports that conclusion.

**Why fcp-mcp:** both changes are explicit FCPXML mutations. Flash-frame
repair extends short clips; gap filling replaces gaps with the asset you
name, so review the timing and chosen media before importing the result.

---

## 3. Captions from .srt

**When to use:** podcast episode, documentary, or long-form with
separately-transcribed captions.

```
You: "Import captions.srt onto V2 with the role 'captions'."
```

**Tools:**

1. Import the SRT. The importer adds title clips on lane 1 with the
   `Titles.Subtitle` role:

```tool-call
{"name":"fcpxml_import_srt","arguments":{"path":"hero.fcpxml","srt_path":"captions.srt","output_path":"hero_captioned.fcpxml"}}
```

2. Check the transformed document:

```tool-call
{"name":"fcpxml_check_safe_zones","arguments":{"path":"hero_captioned.fcpxml"}}
```

3. If pre-existing clips named `Caption ...` need a different role,
   use JSON matching rules:

```tool-call
{"name":"fcpxml_batch_assign_roles","arguments":{"path":"hero_captioned.fcpxml","rules_json":"[{\"match\":\"Caption\",\"role\":\"Titles.Caption\"}]","output_path":"hero_captioned.fcpxml"}}
```

**Why fcp-mcp:** `.srt` → FCPXML title clips has nasty edge cases
(sub-frame drift, overlapping captions, non-ASCII). `import_srt` handles
them deterministically.

---

## 4. Beat-informed rough cut

**When to use:** music video, promo, or any edit where shot cadence
should approximate musical beat spacing.

```
You: "Build a 60-second rough cut from shots_01–12, synced to song.mp3."
```

**Tools:**

1. Detect beat timestamps:

```tool-call
{"name":"media_detect_beats","arguments":{"path":"song.mp3"}}
```

2. Measure loudness separately:

```tool-call
{"name":"media_loudness","arguments":{"path":"song.mp3"}}
```

3. Calculate a representative inter-beat interval, then use it as the
   maximum clip duration:

```tool-call
{"name":"fcpxml_auto_rough_cut","arguments":{"clips_json":"[{\"src\":\"shots_01.mov\",\"name\":\"Shot 01\",\"duration\":\"5s\"},{\"src\":\"shots_02.mov\",\"name\":\"Shot 02\",\"duration\":\"5s\"}]","target_duration":"60s","max_clip_duration":"2s","project_name":"Promo","output_path":"promo.fcpxml"}}
```

4. Add the audio asset:

```tool-call
{"name":"fcpxml_add_audio","arguments":{"path":"promo.fcpxml","audio_src":"song.mp3","position":"end","output_path":"promo_with_audio.fcpxml"}}
```

5. Confirm the resulting duration:

```tool-call
{"name":"fcpxml_timeline_stats","arguments":{"path":"promo_with_audio.fcpxml"}}
```

**Why fcp-mcp:** The current tools can derive a beat cadence and use it to constrain
shot length. It does not yet accept individual beat cut points, so this
is an approximation rather than frame-exact beat placement.

---

## 5. Long-form interview diagnostics and gap cleanup

**When to use:** inspect source silence, remove long FCPXML gap elements,
and add known review markers. This workflow does not transcribe or cut
source-media silence intervals.

```
You: "Clean up interview_raw.fcpxml — silences, pauses, mark every 'um'."
```

**Tools:**

1. Identify the interview clip and source path:

```tool-call
{"name":"fcpxml_list_clips","arguments":{"path":"interview_raw.fcpxml"}}
```

2. Detect silence in that source:

```tool-call
{"name":"media_detect_silence","arguments":{"path":"interview.mov","noise_threshold":"-40dB","min_duration":0.8}}
```

3. Independently remove FCPXML gap elements at least 0.8 seconds long:

```tool-call
{"name":"fcpxml_remove_silence","arguments":{"path":"interview_raw.fcpxml","silence_threshold_seconds":0.8,"output_path":"interview_clean.fcpxml"}}
```

4. Optionally add review markers from known timecodes:

```tool-call
{"name":"fcpxml_batch_add_markers","arguments":{"path":"interview_clean.fcpxml","markers_json":"[{\"clip_name\":\"Interview A\",\"start\":\"30s\",\"value\":\"Review filler word\"}]","output_path":"interview_clean.fcpxml"}}
```

5. Compare duration:

```tool-call
{"name":"fcpxml_timeline_stats","arguments":{"path":"interview_clean.fcpxml"}}
```

**Why fcp-mcp:** FFmpeg-backed silence analysis and deterministic
timeline-gap removal are available in the same server, while their
distinct semantics remain explicit.

---

## 6. YouTube chapter markers from FCP markers

**When to use:** documentary, podcast, or tutorial with marker-driven
chapter navigation on YouTube.

```
You: "Emit YouTube chapter timestamps from the chapter markers on hero.fcpxml."
```

**Tools:**

```tool-call
{"name":"fcpxml_list_markers","arguments":{"path":"hero.fcpxml"}}
```

The agent reformats the returned markers into YouTube's `MM:SS Label`
format, one per line.

**Why fcp-mcp:** no FCP share destination produces the YouTube chapter
format. This is a one-call workflow — the agent does the formatting,
fcp-mcp does the data.

---

## 7. Multicam sync + proxy round-trip

**When to use:** event coverage with 2–6 cameras + proxy edit on a
laptop, online on a workstation.

```
You: "Check that all proxy clips have originals, then export for online."
```

**Tools:**

1. Identify media references:

```tool-call
{"name":"fcpxml_list_clips","arguments":{"path":"event_proxy.fcpxml"}}
```

2. Flag missing referenced files:

```tool-call
{"name":"fcpxml_check_media_links","arguments":{"path":"event_proxy.fcpxml"}}
```

3. Export for Resolve after links are valid:

```tool-call
{"name":"fcpxml_export_resolve","arguments":{"path":"event_proxy.fcpxml","output_path":"event_online.xml"}}
```

**Why fcp-mcp:** link checking happens before the cross-NLE export.
There is no relink tool; repair missing paths in Final Cut Pro or the
source FCPXML before exporting.

---

## 8. Compressor batch encode matrix

**When to use:** one master → N deliverable formats (ProRes master, H.264
web, HEVC mobile, AAC-only audio, etc.).

```
You: "Bounce hero.mov to the four deliverable presets I have configured."
```

**Tools:**

1. List installed presets and Compressor CLI information:

```tool-call
{"name":"compressor_list_settings","arguments":{}}
```

2. Submit each selected `.cmprstng` file:

```tool-call
{"name":"compressor_encode","arguments":{"input_path":"hero.mov","setting_path":"Presets/Apple ProRes 422 HQ.cmprstng","output_dir":"deliverables","batch_name":"ProRes master"}}
```

```tool-call
{"name":"compressor_encode","arguments":{"input_path":"hero.mov","setting_path":"Presets/YouTube 4K.cmprstng","output_dir":"deliverables","batch_name":"YouTube 4K"}}
```

```tool-call
{"name":"compressor_encode","arguments":{"input_path":"hero.mov","setting_path":"Presets/HEVC Mobile 1080p.cmprstng","output_dir":"deliverables","batch_name":"HEVC mobile"}}
```

```tool-call
{"name":"compressor_encode","arguments":{"input_path":"hero.mov","setting_path":"Presets/Audio AAC 320k.cmprstng","output_dir":"deliverables","batch_name":"AAC audio"}}
```

**Why fcp-mcp:** each call invokes the checked Compressor CLI and
reports its captured submission output. The current tool does not track encode
completion or normalize the output into a portable job-ID schema.
