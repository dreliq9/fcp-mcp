# Final Cut Pro 12.3 compatibility and MCP gap audit

Date: 2026-08-11
Final Cut Pro inspected: 12.3 (build 450152)
MCP baseline: fcp-mcp 0.3.0, upstream `main`
Audit window: Final Cut Pro 11.0 through 12.3

## Executive finding

The baseline MCP was not Final Cut 12-aware. It could send generic menu paths
and shortcuts, but it defaulted generated XML to FCPXML 1.11, documented parser
support only through 1.11, discarded smart collections, and had no discoverable
contract for the new Transcript Search, Visual Search, or Final Cut 12 editing
workflows.

This branch closes the machine-addressable gaps:

- generated FCPXML now defaults to 1.14;
- the parser inventories smart collections and preserves every predicate;
- `fcpxml_add_search_collection` writes validated Transcript or Visual Search
  collections using `match-text` and `match-analysis-type`;
- `fcp_final_cut_12` exposes typed actions for the callable features introduced
  from 11.0 through 12.3, including browser search; and
- live results distinguish a sent command from an interactive workflow and do
  not claim that Final Cut completed an AI operation.

The remaining gaps are limitations of Final Cut's automation surface, external
dependencies, or features that are automatic application behavior rather than
operations an MCP should reproduce.

## Authoritative sources

- [Final Cut Pro release notes](https://support.apple.com/en-gb/102825)
- [What’s new in Final Cut Pro](https://support.apple.com/en-lamr/guide/final-cut-pro/ver19637ebab/mac)
- [Search for clips and projects](https://support.apple.com/en-lamr/guide/final-cut-pro/ver65764b45/12.0/mac/15.6)
- [Edit to the beat](https://support.apple.com/en-lamr/guide/final-cut-pro/ver65b55a2b7/12.0/mac/15.6)
- [Generate subtitles automatically](https://support.apple.com/en-lamr/guide/final-cut-pro/verd6d52c4e8/12.3/mac/15.6)
- [Add an Auto Mask](https://support.apple.com/en-lamr/guide/final-cut-pro/ver6c261d07c/12.3/mac/15.6)
- [Match color between clips](https://support.apple.com/en-lamr/guide/final-cut-pro/verbbd35b3e/12.3/mac/15.6)
- [Detect and restore edits](https://support.apple.com/en-lamr/guide/final-cut-pro/verfe2eedc5f/12.3/mac/15.6)
- [FCPXML Reference](https://developer.apple.com/documentation/professional-video-applications/fcpxml-reference)

The installed Final Cut bundle's `FCPXMLv1_13.dtd` and
`FCPXMLv1_14.dtd` were also compared directly. The 1.14 schema adds
`match-analysis-type`, the `isRelatedTo` text rule, and the `transcript`,
`visual`, and `all-text` text scopes. No other content-model changes exist
between those two DTDs.

## Release-by-release coverage

Status meanings:

- **Typed**: a named action or FCPXML contract exists and is tested.
- **Generic/native**: the MCP can invoke or carry the feature, but Final Cut or
  macOS owns its behavior and there is no feature-specific result to inspect.
- **Bounded**: an entry point exists, but Final Cut requires human interaction
  or exposes no result state.
- **No MCP work**: a fix, default, or application-only behavior needs no MCP
  equivalent.

| Version | Apple feature or enhancement | MCP coverage after this work | Status / boundary |
|---|---|---|---|
| 11.0 | Magnetic Mask | `fcp_final_cut_12(add_magnetic_mask)` | Bounded: starts the mask workflow; object selection, analysis, and refinement remain in Final Cut. |
| 11.0 | Transcribe to Captions | `generate_closed_captions` | Bounded: opens the Generate Captions workflow; the editor reviews settings and confirms. |
| 11.0 | Spatial video editing | Existing import, FCPXML, media inspection, and share surfaces | Generic/native: there is no stable live API for stereo inspector controls. |
| 11.0 | 90, 100, and 120 fps timelines | Rational-time parser/generator and arbitrary format resources | Generic/native; no hard-coded frame-rate ceiling was found. |
| 11.0 | Picture in Picture, Callout, and Modular content | `fcp_discover_effects`, `fcp_list_motion_templates`, generic FCPXML effect references | Generic/native. |
| 11.0 | Vertical clip movement and other shortcuts | `fcp_keyboard_shortcut` | Generic; callers can use installed command-set shortcuts. |
| 11.0 | Third-party Media Extensions | macOS/Final Cut media decoding | No MCP work: format support is supplied by installed extensions. |
| 11.0 | FCPXML 1.13 | Parser accepts and preserves supported structures | Typed compatibility. |
| 11.1 | Adjustment clips | `add_adjustment_clip` | Typed command sent; clip creation is not independently observed. |
| 11.1 | Image Playground | `open_image_playground` | Bounded: creation and insertion remain interactive. |
| 11.1 | Magnetic Mask improvements | Same Magnetic Mask action plus generic shortcut/menu gateways | Bounded. |
| 11.1 | Quantec QRS and renamed audio effects | Effect discovery | Generic/native. |
| 11.1 | Reveal multicam/sync source | `reveal_source_in_browser` | Typed command sent. |
| 11.1 | Drag markers and single-role caption transcription | Existing marker and caption surfaces | Generic/native. |
| 11.2 | iPhone ProRes RAW controls | Existing media inspection only | Gap: exposure, temperature, tint, and demosaicing inspector values have no stable AppleScript or typed FCPXML control in this MCP. |
| 11.2 | Apple Log 2 LUT | Effect/LUT discovery and generic FCPXML references | Gap: automatic LUT choice and inspector state are not observed. |
| 12.0 | Transcript Search | `set_browser_search` with exact `includes` or semantic `is_related_to`; FCPXML 1.14 saved collections | Typed criteria, bounded results: matching rows are not exposed to Accessibility or AppleScript. |
| 12.0 | Visual Search | `set_browser_search` with `visual`; FCPXML 1.14 saved collections | Typed criteria, bounded results for the same reason. |
| 12.0 | Beat Detection and grid | `toggle_beat_detection`, `toggle_beat_grid` | Typed commands. Existing `media_detect_beats` remains explicitly separate offline audio analysis. |
| 12.0 | Guides, toolbar creation, and demo project | `download_demo_project`; existing project/library tools | Bounded download; guides and toolbar layout are application UI. |
| 12.0 | FCPXML 1.14 | 1.14 generator default, parser inventory, saved-search writer | Typed and DTD-canary tested. |
| 12.2 | Magnetic Timeline tutorial | `open_magnetic_timeline_tutorial` | Bounded interactive tutorial. |
| 12.2 | Startup and Beat Detection crash fixes | Final Cut application behavior | No MCP work. |
| 12.3 | Generate Captions subtitles | `generate_subtitles` | Bounded: Apple silicon and U.S. English restrictions apply; the editor confirms settings. |
| 12.3 | Auto Mask | `add_auto_mask` | Bounded: object/region selection and Add remain interactive. |
| 12.3 | AI Match Color | `match_color` | Bounded: reference-frame selection and Apply Match remain interactive. |
| 12.3 | Edit Detection | `detect_edits` | Typed command sent; resulting cuts are not enumerated through the live API. |
| 12.3 | Persistent two-up trimming | `toggle_two_up_display` | Typed command. |
| 12.3 | Send frame to Pixelmator Pro | `send_frame_to_pixelmator_pro` | Bounded and requires Pixelmator Pro. It was not installed on the audited Mac. |
| 12.3 | Move/swap primary-storyline clips | `move_primary_left`, `move_primary_right` | Typed commands. |
| 12.3 | Select primary clip with connected media | `select_connected_clips` | Typed command. |
| 12.3 | HEVC proxy generation | `open_transcode_media` | Bounded: opens the dialog. HEVC is Final Cut's new native proxy policy; the result codec is not observable. |
| 12.3 | Background rendering Off by default | Final Cut preference/default | No MCP work. |
| 12.3 | Convert captions to subtitles | `duplicate_captions_to_subtitles` | Typed menu command. |
| 12.3 | Select and batch-adjust subtitles | `select_all_subtitles`, then generic inspector/menu control | Gap: Command-A is focus-sensitive and inspector parameter values are not exposed as a stable typed API. The result warns about this precondition. |
| 12.3 | Creator Themes content | `open_content_library` | Bounded: download confirmation and installed-content verification remain in Final Cut. |
| 12.3 | Listed bug fixes | Final Cut application behavior | No MCP work. |

## New public contracts

### `fcpxml_add_search_collection`

Inputs include collection name, `transcript` or `visual`, optional query,
`includes` or `isRelatedTo`, analysis availability rule, event, and output.
The operation upgrades the declaration to FCPXML 1.14, commits atomically,
re-parses the candidate, and returns a transaction receipt.

### `fcp_final_cut_12`

The action enum makes current features discoverable without making clients
construct English menu paths. Search uses stable Accessibility identifiers for
the browser search field and category button. Other actions use Apple's
documented shortcuts or menu commands observed in Final Cut Pro 12.3.

All live mutations retain `verification_status=unverified`. Browser search is
slightly stronger: it observes the entered query and selected criteria, but
still reports `resultsObserved=false` because result rows are unavailable.

## Remaining engineering gaps and recommended follow-up

1. **Live result inspection.** Final Cut's AppleScript dictionary exposes
   libraries, events, and projects, not AI-analysis output, selected clips,
   search-result rows, or inspector values. End-to-end verification will
   require a more extensive Accessibility reader and will remain UI/version
   sensitive.
2. **Localization.** Stable search control identifiers are used where Final
   Cut exposes them. Menu commands still depend on English menu names. Add a
   localized menu-name map before claiming non-English live support.
3. **Inspector automation.** ProRes RAW controls, Apple Log 2 selection,
   subtitle batch formatting, and detailed mask/color parameters need typed
   Accessibility adapters. These should be implemented only with exact
   post-action observations; blind coordinate clicking would weaken the MCP's
   trust model.
4. **External apps and downloads.** Pixelmator Pro, Image Playground, demo
   media, and Creator Themes require installed software/content and user
   confirmation. Doctor checks could report these dependencies in a later
   release.
5. **Search result export.** FCPXML 1.14 can persist search criteria but does
   not serialize Transcript/Visual analysis results. There is no supported
   schema-level way to return the matching moments themselves.

## Verification completed

- compared installed Apple FCPXML 1.13 and 1.14 DTDs;
- DTD-validated a minimal 1.14 Transcript Search collection;
- inspected the actual 12.3 menus and browser Accessibility identifiers;
- added parser, writer, result-contract, action-mapping, hostile-input, profile,
  catalog, and release-smoke tests; and
- live browser-search canary limited to reversible search UI state.
