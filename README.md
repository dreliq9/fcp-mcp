# fcp-mcp — Final Cut Pro MCP Server

<!-- mcp-name: io.github.dreliq9/fcp-mcp -->

**The most capable MCP server for Final Cut Pro** — 88 tools covering
FCPXML editing, live FCP control, parametric puppets, and media analysis.
One server, every layer.

```
You: "Open the library, find flash frames in the hero timeline, fix them, and bounce to ProRes."
Claude → fcp_open_library → fcpxml_detect_flash_frames → fcpxml_fix_flash_frames
       → fcp_import_xml → fcp_share → compressor_encode
Result: fixed FCPXML + ProRes master, verified in-app
```

Where the competition stops at one layer, fcp-mcp covers three:

- **FCPXML engine** — parse, edit, QC, generate, cross-NLE export — 49 tools
- **Live FCP control** — AppleScript-backed library/playback/menu/share — 20 tools
- **Media analysis** — ffprobe-backed scene/loudness/frame tools — 10 tools
- **Parametric puppets** — character animation presets in FCPXML — 7 tools
- **Compressor** — dispatch encodes to Apple Compressor — 2 tools

## Available Tools (88 across 12 categories)

| Category | Count | What it does |
|---|---|---|
| **inspect** | 8 | Parse, list clips/markers/effects/roles, analyze pacing, timeline stats, A/B diff |
| **qc** | 10 | Flash frames, gaps, duplicates, media links, frame rates, audio levels, safe zones, duration, schema, full QC report |
| **edit** | 12 | Markers, keywords, titles, audio, transitions, trim, split, delete, reorder, speed, role assign, reformat |
| **heal** | 3 | Fix flash frames, fill gaps, remove silence |
| **batch** | 4 | Markers, rename, role assign, apply transition across many clips |
| **generate** | 4 | New project/timeline, auto rough cut, montage from a shotlist |
| **templates** | 3 | List, apply, save FCPXML templates (reusable title/audio/role bundles) |
| **io** | 5 | Import SRT/EDL, export EDL + DaVinci Resolve XML + Premiere FCP7 XMEML |
| **live** | 20 | AppleScript-backed: library/events/projects, playback, menu/keyboard, share, discover effects & motion templates |
| **puppet** | 7 | Parametric character rigs in FCPXML with motion presets (walk, talk, wave, multi-scene composition) |
| **media** | 10 | ffprobe + ffmpeg: info, streams, EBU R128 loudness, silence, beat detection, scene detect, thumbnails, audio-to-MIDI |
| **compressor** | 2 | List Compressor settings, dispatch encode jobs |

## Project Structure

```
fcp-mcp/
├── src/fcp_mcp/
│   ├── __init__.py
│   ├── __main__.py              # python -m fcp_mcp.server
│   ├── server.py                # FastMCP entry + all 88 tool handlers
│   ├── fcpxml/
│   │   ├── parser.py            # FCPXML → Python object tree
│   │   ├── writer.py            # object tree → FCPXML (lossless)
│   │   ├── models.py            # TimeValue, Timecode, Clip, Timeline, etc.
│   │   ├── time_utils.py        # rational-arithmetic timecode
│   │   ├── analysis.py          # pacing, flash frames, gaps, duplicates
│   │   ├── validator.py         # DTD-style structural validation
│   │   ├── generator.py         # programmatic project/timeline creation
│   │   ├── puppet.py            # character puppet system
│   │   └── diff.py              # timeline A/B comparison
│   ├── fcp_control/             # AppleScript bridge for live FCP control
│   ├── media/
│   │   └── ffprobe.py           # ffprobe wrapper for media analysis
│   ├── pipeline/                # multi-step workflows
│   └── utils/
│       ├── safe_xml.py          # defusedxml hardening
│       └── paths.py             # path resolution, expansion
├── tests/                       # 107 tests
├── examples/
│   ├── quickstart.py            # install verification
│   └── GALLERY.md               # workflow gallery with prompts
├── pyproject.toml
├── server.json                  # MCP Registry manifest
├── smithery.yaml                # Smithery directory config
├── LLM_GUIDE.md                 # operational guide for agents
├── WORKFLOWS.md                 # production recipes
├── CHANGELOG.md
├── ROADMAP.md
├── CONTRIBUTING.md
└── LICENSE
```

---

## Setup

### Prerequisites

- Python 3.10+
- macOS with Final Cut Pro (for live-control tools — FCPXML tools work anywhere)
- **FFmpeg** on `$PATH` (for `media_*` tools): `brew install ffmpeg`
- **Apple Compressor** (optional, for `compressor_*` tools)

### Install

```bash
pipx install fcp-mcp
```

Or from source (for contributors):

```bash
git clone https://github.com/dreliq9/fcp-mcp.git
cd fcp-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Verify

```bash
source .venv/bin/activate
python -c "import fcp_mcp; print(fcp_mcp.__version__)"
pytest tests/ -v
```

### Connect to Claude Code

```bash
claude mcp add-json fcp '{"type":"stdio","command":"fcp-mcp"}' --scope user
```

Or if you installed from source, point at your venv's Python:

```bash
claude mcp add-json fcp '{"type":"stdio","command":"/FULL/PATH/TO/.venv/bin/fcp-mcp"}' --scope user
```

Or edit `~/.claude.json` directly:

```json
{
  "mcpServers": {
    "fcp": {
      "type": "stdio",
      "command": "fcp-mcp"
    }
  }
}
```

### Claude Desktop

Add the same config to your Claude Desktop config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

### Verify connection

```bash
claude mcp list       # from terminal
/mcp                  # inside Claude Code
```

---

## Key Features

### Three layers in one server

Most FCP MCPs pick a lane. **DareDev256/fcpxml-mcp-server** is FCPXML-only.
**elliotttate/finalcutpro-mcp** is AppleScript/JXA-heavy with thin FCPXML.
fcp-mcp does both — and adds media analysis (ffprobe) and a parametric
puppet system on top. An agent can open a library, inspect the active
timeline, patch the XML, re-import, trigger Share, and dispatch a
Compressor job — end to end.

### Rational-arithmetic timecode

Time values are stored as fractions (`"720/24s"`) and only collapsed to
floats at display boundaries. Frame-accurate across 23.976 / 24 / 29.97 /
59.94 / drop-frame — no rounding drift when splitting, trimming, or
concatenating.

### Hardened XML parsing

All `.fcpxml` reads go through `defusedxml` via `utils/safe_xml.py` —
XXE, billion-laughs, and external-entity attacks blocked by default.
Size and depth limits enforced. `validator.py` runs structural schema
checks before any destructive operation.

### Cross-NLE export

Same timeline out to three targets:

- `fcpxml_export_resolve` — DaVinci Resolve-flavored XML (v1.9)
- `fcpxml_export_fcp7` — Premiere-compatible XMEML (FCP7 format)
- `fcpxml_export_edl` — flat EDL for color-grading and archive pipelines

### Parametric character puppets

`puppet_*` tools build animated character rigs entirely in FCPXML —
no third-party motion templates required. Parts, keyframes, and
presets (walk, talk, wave, multi-character compositions) emit
standards-compliant XML that opens in any FCP.

### QC before you cut

```
fcpxml_qc_report("hero.fcpxml")
  → flash frames (2 × 1-frame orphans at 00:12:03, 00:14:22)
  → audio-level warnings (3 clips peaking above -3dBFS)
  → offline media (1 stock clip missing)
  → safe-zone violations (title at y=0.94, outside 90% action safe)
  → frame-rate conflicts (none)
```

One call, full catalog of issues with timecodes, before the edit leaves
your machine.

### Live FCP control

When FCP is running, `fcp_*` tools wire through AppleScript:

```
fcp_open_library(...)
fcp_get_timeline_info()    → current project, active range, playhead
fcp_playback("play" | "pause" | "goto" | "in_out")
fcp_menu_command("File > Export > Export Using Compressor Settings...")
fcp_keyboard_shortcut("cmd+shift+e")
fcp_share("YouTube — 4K")
fcp_discover_effects()     → every installed effect/transition/title
```

---

## Output Files

By default, modified FCPXMLs are written next to their input with a
`_modified` suffix. Override per-call via the `output_path` parameter,
or globally with:

```bash
export FCP_MCP_OUTPUT_DIR=/your/path
```

---

## Examples

See the [full gallery](examples/GALLERY.md) for tool sequences and
workflow breakdowns. Sample prompts:

- *"Run a full QC report on hero.fcpxml and fix every flash frame."*
- *"Convert these .srt captions into FCPXML title clips on the V2 track."*
- *"Take my assembly-edit XML and re-export it as a DaVinci Resolve XML for color."*
- *"List every clip on the timeline, then batch-assign the 'dialogue' role to all interview clips."*
- *"Open the active library, seek to the first marker, and dispatch a ProRes 422 HQ bounce via Compressor."*
- *"Build a 3-character puppet scene: one walking, one talking, one waving. 5-second timeline."*

---

## Architecture

```
Claude Code / Claude Desktop / any MCP client
        │
        │  stdio (JSON-RPC)
        ▼
   fcp-mcp server (fcp_mcp/server.py, FastMCP)
        │
        ├── fcpxml/parser + writer + models ← rational-arithmetic, defusedxml-hardened
        ├── fcpxml/analysis + validator     ← QC, pacing, flash frames, gaps
        ├── fcpxml/generator + puppet       ← programmatic timeline construction
        ├── fcpxml/diff                     ← A/B timeline comparison
        ├── fcp_control/ (AppleScript)      ← live FCP when available
        ├── media/ffprobe                   ← clip info, loudness, scenes, frames
        └── Compressor (CLI dispatch)       ← automated encodes
              │
              ▼
        .fcpxml (v1.11+) / EDL / DaVinci XML / FCP7 XMEML / ProRes / H.264
```

---

## Troubleshooting

**`fcp-mcp: command not found`** — make sure the venv you installed into is on
your PATH, or use the absolute path to the venv's `bin/fcp-mcp` in your MCP config.

**`ffmpeg: command not found` on media_* calls** — `brew install ffmpeg`. FFmpeg
is not bundled.

**`fcp_*` tools return "Final Cut Pro is not running"** — launch FCP first. Live
tools require an active FCP process on macOS.

**`fcp_*` tools fail silently on newer FCP versions** — FCP's scripting surface is
limited compared to pre-FCPX versions. Menu items and keyboard shortcuts are the
reliable path; some JXA queries are gated behind Accessibility permissions
(System Settings → Privacy & Security → Accessibility → Claude/Terminal).

**`fcpxml_*` tools complain about schema** — Make sure your input is FCPXML 1.10+.
Run `fcpxml_validate` first. For older exports, open and re-save the library in
FCP 10.6+ before processing.

**Tests failing on import** — activate the venv and reinstall: `pip install -e ".[dev]"`.

---

## Planned Work

See [ROADMAP.md](ROADMAP.md) for the full plan. Highlights:

- **MCP Prompts** — shipped: `qc-check`, `youtube-chapters`, `cleanup`, `rough-cut`, `beat-sync`
- **Windows/Linux parity for FCPXML tools** — live-control tools remain macOS-only
- **Proxy/Resolve round-trip** — proxy-aware offline/online workflows

---

## Acknowledgments

fcp-mcp was co-developed by Adam Steen and [Claude](https://claude.ai) (Anthropic).

## License

MIT — see [LICENSE](LICENSE).
