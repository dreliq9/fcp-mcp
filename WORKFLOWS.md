# fcp-mcp Workflows

Production recipes using fcp-mcp tools. Each workflow lists the agent
prompt, the tool sequence, and what makes the workflow non-trivial
without fcp-mcp.

For a one-shot verification that your install works, see
[`examples/quickstart.py`](examples/quickstart.py). For a visual gallery
with renders and narratives, see [`examples/GALLERY.md`](examples/GALLERY.md).

---

## 1. Full QC pass on a locked cut

**When to use:** right before you bounce a master. Catches flash frames,
gaps, media drift, level problems, and safe-zone breaches in one pass.

```
You: "Run a full QC on hero.fcpxml and give me the summary."
```

**Tools (one call):**
- `fcpxml_qc_report(path="hero.fcpxml")`

**What you get back:** structured JSON with `flash_frames`, `gaps`,
`duplicates`, `media_offline`, `frame_rate_conflicts`,
`audio_level_warnings`, `safe_zone_violations`. Each entry has a
timecode, clip name, and severity.

**Why fcp-mcp:** no FCP extension runs this many checks in one shot.
Without fcp-mcp, you'd script six separate queries against the FCPXML
DOM.

---

## 2. Heal pass after QC

**When to use:** follow-up to recipe 1. Non-destructive automatic fixes
for the most common QC hits.

```
You: "Fix the flash frames and close the gaps, then re-run QC."
```

**Tools:**
1. `fcpxml_fix_flash_frames(path="hero.fcpxml", output_path="hero_heal.fcpxml")`
2. `fcpxml_fill_gaps(path="hero_heal.fcpxml", output_path="hero_heal.fcpxml")`
3. `fcpxml_qc_report(path="hero_heal.fcpxml")` — re-verify

**What you get back:** the healed FCPXML plus a clean QC report.

**Why fcp-mcp:** fix tools are structural reshuffles — they never
re-time the neighbors. Hand-fixing flash frames in FCP is tedious and
error-prone.

---

## 3. Captions from .srt

**When to use:** podcast episode, documentary, or long-form with
separately-transcribed captions.

```
You: "Import captions.srt onto V2 with the role 'captions'."
```

**Tools:**
1. `fcpxml_import_srt(srt_path="captions.srt", target="hero.fcpxml", lane=2)`
2. `fcpxml_check_safe_zones(path="hero.fcpxml")` — verify positioning
3. `fcpxml_batch_assign_roles(path="hero.fcpxml", pattern="Caption *",
   role="captions")`

**Why fcp-mcp:** `.srt` → FCPXML title clips has nasty edge cases
(sub-frame drift, overlapping captions, non-ASCII). `import_srt` handles
them deterministically.

---

## 4. Beat-synced rough cut

**When to use:** music video, promo, or any edit where cuts should
land on musical beats.

```
You: "Build a 60-second rough cut from shots_01–12, synced to song.mp3."
```

**Tools:**
1. `media_detect_beats(path="song.mp3")` — returns beat timecodes
2. `media_loudness(path="song.mp3")` — confirm broadcast-safe LUFS
3. `fcpxml_auto_rough_cut(shots=[...], cut_points=<beats from step 1>,
   output_path="promo.fcpxml")`
4. `fcpxml_add_audio(path="promo.fcpxml", audio="song.mp3", lane=-1)`
5. `fcpxml_timeline_stats(path="promo.fcpxml")` — confirm duration

**Why fcp-mcp:** beat detection + cut placement in one agent turn. No
manual tap-in, no DAW round-trip.

---

## 5. Long-form interview cleanup

**When to use:** 60-minute raw interview → tight cut. Removes silences,
filler words (via markers), and pauses.

```
You: "Clean up interview_raw.fcpxml — silences, pauses, mark every 'um'."
```

**Tools:**
1. `fcpxml_list_clips(path="interview_raw.fcpxml")` — identify the
   interview clip(s)
2. `media_detect_silence(path="<clip_source>.mov", threshold=-40, duration=0.8)`
3. `fcpxml_remove_silence(path="interview_raw.fcpxml",
   silences=<from step 2>, output_path="interview_clean.fcpxml")`
4. (Optional) `fcpxml_batch_add_markers(path="interview_clean.fcpxml",
   markers=[<filler-word timecodes>])`
5. `fcpxml_timeline_stats(path="interview_clean.fcpxml")` — duration delta

**Why fcp-mcp:** silence detection at FCP-level doesn't exist. `media_detect_silence`
reuses ffmpeg's silence detection and hands timecodes straight back to
`fcpxml_remove_silence`.

---

## 6. YouTube chapter markers from FCP markers

**When to use:** documentary, podcast, or tutorial with marker-driven
chapter navigation on YouTube.

```
You: "Emit YouTube chapter timestamps from the chapter markers on hero.fcpxml."
```

**Tools:**
1. `fcpxml_list_markers(path="hero.fcpxml")` — returns all markers with
   timecode + label
2. (Agent reformats the list into YouTube's `MM:SS Label` format,
   one per line)

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
1. `fcpxml_list_clips(path="event_proxy.fcpxml")` — identify proxy
   references
2. `fcpxml_check_media_links(path="event_proxy.fcpxml")` — flag offline
3. (Optional v0.4) `fcpxml_relink_media(path="event_proxy.fcpxml",
   search_root="/Volumes/ORIGINALS")`
4. `fcpxml_export_resolve(path="event_proxy.fcpxml",
   output_path="event_online.xml")` — or `_export_fcp7` for Premiere

**Why fcp-mcp:** offline/online awareness is first-class. No more
opening the project on the online workstation to find missing media.

---

## 8. Compressor batch encode matrix

**When to use:** one master → N deliverable formats (ProRes master, H.264
web, HEVC mobile, AAC-only audio, etc.).

```
You: "Bounce hero.mov to the four deliverable presets I have configured."
```

**Tools:**
1. `compressor_list_settings()` — list installed presets
2. `compressor_encode(input="hero.mov", setting="Apple ProRes 422 HQ")`
3. `compressor_encode(input="hero.mov", setting="YouTube 4K")`
4. `compressor_encode(input="hero.mov", setting="HEVC Mobile 1080p")`
5. `compressor_encode(input="hero.mov", setting="Audio AAC 320k")`

**Why fcp-mcp:** each `compressor_encode` call returns immediately with
a job ID — the agent dispatches all four in parallel and Compressor
handles queuing. No FCP dialog box scripting.
