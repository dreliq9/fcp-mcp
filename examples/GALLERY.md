# Example Gallery

Real workflows built through fcp-mcp tools. Each example leans on a
feature that sets fcp-mcp apart: full-catalog QC, heal-then-verify, the
puppet system, and end-to-end FCPXML → live FCP → Compressor pipelines.

---

## Full QC + heal for a 20-minute doc cut

**Prompt:** *"Run a full QC on hero.fcpxml, fix the flash frames and gaps, then give me the summary."*

**Tool sequence:**
1. `fcpxml_qc_report("hero.fcpxml")` — full catalog: flash frames, gaps,
   duplicates, media links, frame rates, audio peaks, safe zones, duration
2. `fcpxml_fix_flash_frames(...)` — merges <2-frame orphans into neighbors
3. `fcpxml_fill_gaps(...)` — extends preceding clip to close silent gaps
4. `fcpxml_qc_report(...)` — re-run to verify the fix applied
5. `fcpxml_timeline_stats(...)` — confirm duration unchanged (±1 frame)

**What makes this an fcp-mcp example:**
- `qc_report` is one call, not seven — the agent gets the whole picture before
  deciding what to fix
- Heal-then-re-QC confirms the fix applied, not just that the tool ran
- Duration delta check catches accidental ripple edits

---

## Captions from .srt to FCPXML titles

**Prompt:** *"Import captions.srt onto the V2 track of hero.fcpxml with the 'caption-safe' font preset."*

**Tool sequence:**
1. `fcpxml_import_srt("captions.srt", target="hero.fcpxml", lane=2)` —
   emits a title clip per caption with in/out matching the .srt timecodes
2. `fcpxml_check_safe_zones("hero.fcpxml")` — verify every title falls
   inside 90% action-safe
3. `fcpxml_batch_assign_roles(...)` — assign the captions role to every
   new title in one call

**What makes this an fcp-mcp example:**
- `import_srt` handles the messy parts (overlapping captions, sub-frame
  drift, non-ASCII characters) without the agent having to reason about
  timecode arithmetic
- `batch_assign_roles` replaces 40+ individual role-assign calls

---

## Beat-synced rough cut from a song + shotlist

**Prompt:** *"Build a 60-second rough cut from these 12 shots, synced to the beat of song.mp3."*

**Tool sequence:**
1. `media_detect_beats("song.mp3")` — returns beat timecodes (ffmpeg-based
   onset detection)
2. `media_loudness("song.mp3")` — checks the music sits at a broadcast-safe
   LUFS before building around it
3. `fcpxml_auto_rough_cut(shots=[...], cut_points=[... from beats ...])` —
   cuts each shot at the nearest beat
4. `fcpxml_add_audio("song.mp3", lane=-1)` — music on A1 lane
5. `fcpxml_timeline_stats(...)` — verify total duration matches the target

**What makes this an fcp-mcp example:**
- `media_detect_beats` eliminates the manual tap-in — the agent reads the
  beats and the rough cut is ready to nudge
- `auto_rough_cut` with explicit cut points is deterministic, unlike
  AI-guess cuts

---

## DaVinci Resolve round-trip for a graded master

**Prompt:** *"Send the locked cut to Resolve for color, keep the audio mix on our side."*

**Tool sequence:**
1. `fcpxml_check_media_links("hero.fcpxml")` — confirm all source files
   are online before exporting
2. `fcpxml_export_resolve("hero.fcpxml", output_path="hero_for_color.xml")` —
   Resolve-flavored XML (v1.9) with roles mapped to tracks
3. (Color happens in Resolve)
4. `fcpxml_import_edl("graded.edl", target="hero.fcpxml")` — optional:
   conform color decisions back as EDL markers for reference

**What makes this an fcp-mcp example:**
- `export_resolve` outputs Resolve-flavored XML with the right flavor of
  tag — not a plain FCPXML that Resolve halfway understands
- Media-link check prevents the "offline media in Resolve" rabbit hole

---

## End-to-end: FCPXML edit → import → Share → Compressor

**Prompt:** *"Here's the cleaned-up FCPXML. Import it into the open FCP library and start a ProRes 422 HQ master."*

**Tool sequence:**
1. `fcp_is_running()` — guard: bail if FCP isn't open
2. `fcp_get_libraries()` — find the target library
3. `fcp_import_xml("hero_clean.fcpxml", library="Hero Doc.fcpbundle")`
4. `fcp_list_share_destinations()` — find "Apple ProRes 422 HQ" preset
5. `fcp_share("Apple ProRes 422 HQ")` — trigger Share
6. `compressor_list_settings()` — optional: inspect available encode targets
7. `compressor_encode(input="hero_clean.mov", setting="ProRes 4444 XQ")` —
   queue a higher-quality alt master in the background

**What makes this an fcp-mcp example:**
- Spans all four layers (FCPXML edit → live FCP → Share → Compressor)
  in one agent turn
- Each tool returns a clean status string the LLM can act on, not
  UI-scraped text

---

## Multi-character puppet scene from scratch

**Prompt:** *"Build a 10-second scene: one walker, one talker, one waving, no source footage."*

**Tool sequence:**
1. `puppet_list_presets()` — see available motion presets
2. `puppet_multi_scene(rigs=[{"name":"a","preset":"walk"},
   {"name":"b","preset":"talk","offset":1}, {"name":"c","preset":"wave","offset":2}],
   duration="10s")`
3. `puppet_build_scene(output_path="scene.fcpxml")` — emit final FCPXML
4. `fcpxml_validate("scene.fcpxml")` — confirm the generated XML passes
   structural checks
5. `fcp_import_xml("scene.fcpxml")` — load into FCP to preview

**What makes this an fcp-mcp example:**
- Puppet timelines are pure FCPXML — no Motion templates, no plugins, no
  rigging apps required
- The entire scene exists in ~2KB of XML and renders with FCP's built-in
  Motion compositor
