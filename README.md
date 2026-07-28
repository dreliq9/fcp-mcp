# fcp-mcp — Final Cut Pro MCP Server

<!-- mcp-name: io.github.dreliq9/fcp-mcp -->

**A local MCP server for Final Cut Pro** — up to 93 tools and five prompts
covering FCPXML editing, opt-in live FCP control, parametric puppets,
media analysis, transactional edit approval, and runtime diagnostics.

> **v0.3 release candidate:** The transactional workflow release is available
> on the `codex/v0.3-transactional-workflows` branch. It has completed the
> [local release review](docs/reviews/v0.3.0-release-review.md), but is not yet
> tagged or published to PyPI or the MCP Registry.

```
You: "Find and repair flash frames in hero.fcpxml."
Claude → fcpxml_detect_flash_frames → fcpxml_workflow_prepare
       → review semantic diff + candidate hash → fcpxml_workflow_commit
Result: the reviewed candidate is atomically committed with durable evidence
```

Where many integrations stop at one layer, fcp-mcp covers the local
editing stack:

- **FCPXML engine** — parse, edit, QC, generate, cross-NLE export — 49 tools
- **Live FCP control** — AppleScript-backed library/playback/menu/share — 20 tools
- **Media analysis** — ffprobe-backed scene/loudness/frame tools — 10 tools
- **Parametric puppets** — character animation presets in FCPXML — 7 tools
- **Compressor** — dispatch encodes to Apple Compressor — 2 tools
- **Runtime diagnostics** — structured offline/live readiness — 1 tool

## v0.3 Release Candidate

v0.3 changes the default AI-piloting contract from direct mutation to a
bounded, durable workflow:

1. `fcpxml_workflow_prepare` creates and validates a private candidate.
2. The client reviews the semantic diff and exact candidate SHA-256.
3. `fcpxml_workflow_commit` accepts only that hash and atomically publishes the
   approved result.
4. Durable status, events, cancellation, and reconciliation evidence remain
   available after the client session ends.

Direct offline mutators remain available through the `edit` and `full`
profiles. Live Final Cut Pro and Compressor actions require the `full` profile
and the separate `FCP_MCP_ENABLE_LIVE_CONTROL=1` opt-in.

Current documentation and evidence:

- [Documentation index](docs/README.md)
- [Agent operating guide](LLM_GUIDE.md)
- [Production workflow recipes](WORKFLOWS.md)
- [v0.3 changelog](CHANGELOG.md)
- [Release-candidate research](docs/research/2026-07-28-v0.3-release-candidate.md)
- [Local release review](docs/reviews/v0.3.0-release-review.md)

## Catalog Profiles

The default `workflow` profile is the safe AI-piloting surface: 34 tools,
three prompts, and three workflow resource templates. Select a profile with
`FCP_MCP_PROFILE`:

| Profile | Tools | Prompts | Resources | Purpose |
|---|---:|---:|---:|---|
| `inspect` | 30 | 2 | 0 | Offline inspection and diagnostics |
| `workflow` (default) | 34 | 3 | 3 | Inspection plus reviewable transactional edits |
| `edit` | 74 | 5 | 3 | Direct offline mutators plus workflows |
| `full` | 93 | 5 | 3 | All offline, live FCP, media, and Compressor tools |

Live FCP control remains independently disabled unless
`FCP_MCP_ENABLE_LIVE_CONTROL=1`; choosing `full` does not grant that
authority.

## Available Tools (93 in `full`)

The full catalog contains 92 domain tools across 13 functional categories,
plus `fcp_doctor`.

| Category | Count | What it does |
|---|---|---|
| **inspect** | 8 | Parse, list clips/markers/effects/roles, analyze pacing, timeline stats, A/B diff |
| **qc** | 10 | Flash frames, gaps, duplicates, media links, frame rates, audio levels, safe zones, duration, structural validation, aggregate QC report |
| **edit** | 12 | Markers, keywords, titles, audio, transitions, trim, split, delete, reorder, speed, role assign, reformat |
| **heal** | 3 | Fix flash frames, fill gaps, remove silence |
| **batch** | 4 | Markers, rename, role assign, apply transition across many clips |
| **generate** | 4 | New project/timeline, auto rough cut, montage from a shotlist |
| **templates** | 3 | List and save FCPXML templates; `fcpxml_apply_template` remains `unsupported_contract` in v0.3.0 |
| **io** | 5 | Import SRT/EDL, export EDL + DaVinci Resolve XML + Premiere FCP7 XMEML |
| **live** | 20 | AppleScript-backed: library/events/projects, playback, menu/keyboard, share, discover effects & motion templates |
| **puppet** | 7 | Parametric character rigs in FCPXML with motion presets (walk, talk, wave, multi-scene composition) |
| **media** | 10 | ffprobe + ffmpeg: info, streams, EBU R128 loudness, silence, beat detection, scene detect, thumbnails, audio-to-MIDI |
| **compressor** | 2 | List Compressor settings, dispatch encode jobs |
| **workflow** | 4 | Prepare, inspect, hash-approve, commit, or cancel durable edit runs |

## Project Structure

```
fcp-mcp/
├── src/fcp_mcp/
│   ├── __init__.py
│   ├── __main__.py              # python -m fcp_mcp CLI
│   ├── cli.py                   # serve, doctor, doctor --json, --version
│   ├── config.py                # immutable environment configuration
│   ├── contracts.py             # stable errors + structured diagnostics
│   ├── diagnostics.py           # non-mutating runtime readiness checks
│   ├── observability.py         # text/JSON transaction events
│   ├── server.py                # MCP v2 entry and profile-selected handlers
│   ├── tool_metadata.py         # MCP safety annotation presets
│   ├── automation/
│   │   └── osascript.py         # argv-isolated AppleScript/JXA runner
│   ├── security/
│   │   └── paths.py             # allowed-root and output policy
│   ├── fcpxml/
│   │   ├── parser.py            # FCPXML → Python object tree
│   │   ├── writer.py            # transactional XML mutation/writing
│   │   ├── models.py            # TimeValue, Timecode, Clip, Timeline, etc.
│   │   ├── time_utils.py        # rational-arithmetic timecode
│   │   ├── analysis.py          # pacing, flash frames, gaps, duplicates
│   │   ├── validator.py         # DTD-style structural validation
│   │   ├── generator.py         # programmatic project/timeline creation
│   │   ├── puppet.py            # character puppet system
│   │   ├── diff.py              # timeline A/B comparison
│   │   └── transaction.py       # validated atomic FCPXML commits
│   ├── fcp_control/             # AppleScript bridge for live FCP control
│   ├── media/
│   │   └── ffprobe.py           # ffprobe wrapper for media analysis
│   ├── pipeline/                # multi-step workflows
│   ├── workflow/                # durable prepare, approval, commit, recovery
│   └── utils/
│       ├── atomic_write.py       # backup, replace, rollback
│       ├── safe_xml.py          # defusedxml hardening
│       └── paths.py             # trusted system path helpers
├── tests/                       # complete unit + contract suite
├── scripts/                     # docs, coverage, and wheel gates
├── docs/
│   ├── README.md                # documentation map and status
│   ├── research/                # dated research records
│   └── reviews/                 # release evidence and decisions
├── examples/
│   ├── quickstart.py            # install verification
│   └── GALLERY.md               # workflow gallery with prompts
├── pyproject.toml
├── server.json                  # MCP Registry manifest
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
- macOS 15.6 or later with Final Cut Pro
- **FFmpeg** on `$PATH` (for `media_*` tools): `brew install ffmpeg`
- **Apple Compressor** (optional, for `compressor_*` tools)

### Install

Install the current published release from PyPI:

```bash
pipx install fcp-mcp
```

Until v0.3 is tagged and published, install its reviewed candidate directly
from the GitHub branch:

```bash
pipx install "git+https://github.com/dreliq9/fcp-mcp.git@codex/v0.3-transactional-workflows"
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
fcp-mcp --version
fcp-mcp doctor
fcp-mcp doctor --json
python examples/quickstart.py
```

`doctor` exits `0` when ready, `1` when offline service is usable but
optional capabilities are degraded, and `2` when configuration blocks
required runtime behavior. MCP clients can call the same structured
surface through `fcp_doctor`.

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

### Scoped and transactional XML handling

All `.fcpxml` reads go through `defusedxml` via `utils/safe_xml.py` —
XXE, entity expansion, and external entities are blocked by default.
The parser enforces configured XML size and depth limits.

User paths are resolved beneath configured roots after symlink
resolution. FCPXML writes are serialized to a secure temporary file
beside the destination, structurally validated, backed up when replacing
an existing destination, atomically committed, and validated again.
The built-in validator checks the invariants it implements; it is not a
complete Apple schema validator. Import into a disposable Final Cut Pro
project is the authoritative compatibility gate for important outputs.
On a release workstation with Final Cut Pro installed, preflight a candidate
against the DTD matching its declared FCPXML version:

```bash
python scripts/apple_dtd_gate.py path/to/candidate.fcpxml
```

This gate reads the DTD from the installed Final Cut Pro application bundle;
the project does not copy or redistribute Apple's schema. A passing DTD check
does not replace the disposable-project import because Final Cut Pro also
checks media and application-level semantics.

`fcpxml_assign_role`, `fcpxml_batch_assign_roles`, and their workflow
operations assign audio roles. For an `asset-clip`, fcp-mcp writes Apple's
`audioRole` attribute and continues to read legacy `role` values for
compatibility.

### Reviewable transactional edits

The default profile exposes four workflow tools:
`fcpxml_workflow_prepare`, `fcpxml_workflow_status`,
`fcpxml_workflow_commit`, and `fcpxml_workflow_cancel`.

Prepare runs a fixed, bounded edit graph and stores the candidate, semantic
diff, validation evidence, and event chain beneath the private
`FCP_MCP_STATE_DIR`. It does not create or change the public destination.
Review the returned summary and `diff_uri`, then pass the exact returned
`candidate_sha256` to commit as `expected_candidate_sha256`. Client approval
is cryptographically bound to that candidate but is recorded as
`client_unverified_human`; the server cannot independently prove that a human
approved a chat message.

Three read-only templates expose durable evidence:
`fcp-workflow://runs/{run_id}`,
`fcp-workflow://runs/{run_id}/events`, and
`fcp-workflow://runs/{run_id}/diff`. Candidate XML is deliberately private
and is not exposed as a resource. Interrupted commits are assessed from
durable evidence and can be explicitly reconciled with:

```bash
fcp-mcp workflow reconcile RUN_ID --json
```

This is a bounded workflow engine, not a generic graph runtime. It does not
use MCP Tasks or run background autonomous agents.

### Cross-NLE export

Same timeline out to three targets:

- `fcpxml_export_resolve` — DaVinci Resolve-flavored XML (v1.9)
- `fcpxml_export_fcp7` — Premiere-compatible XMEML (FCP7 format)
- `fcpxml_export_edl` — flat EDL for color-grading and archive pipelines

### Parametric character puppets

`puppet_*` tools build animated character rigs entirely in FCPXML —
no third-party motion templates required. Parts, keyframes, and
presets (walk, talk, wave, multi-character compositions) emit XML for
structural validation followed by a disposable-project FCP import gate.

### QC before you cut

```
fcpxml_qc_report("hero.fcpxml")
  → Markdown validation summary and timeline statistics
  → gaps and flash frames with timecodes
  → duplicate sources
  → pacing distribution
```

Media links, frame rates, audio levels, safe zones, and target duration
are separate `fcpxml_check_*` tools. Run the specific checks you need
instead of assuming the aggregate report includes them.

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
or set the canonical directory for relative and generated outputs:

```bash
export FCP_MCP_OUTPUT_DIR=/your/path
```

`FCP_PROJECTS_DIR` remains a legacy fallback when
`FCP_MCP_OUTPUT_DIR` is unset. Relative input paths resolve beneath the
output directory.

Allow additional input roots with the macOS path separator (`:`):

```bash
export FCP_MCP_ALLOWED_ROOTS="/Users/me/Movies:/Volumes/Media"
```

Absolute and symlink-resolved inputs outside those roots are rejected.
For an input-backed operation, explicit outputs may be beneath the
output directory or the input file's parent. Input and output resolving
to the same file are always rejected. Replacing an existing destination
creates a sibling backup named
`<file>.bak.<UTC timestamp>.<transaction UUID>`.

Live FCP, Accessibility, and Compressor actions are opt-in:

```bash
export FCP_MCP_ENABLE_LIVE_CONTROL=1
```

Leave it unset for offline-only use. Runtime and transaction events use
text by default; set `FCP_MCP_LOG_FORMAT=json` for JSON lines.

Workflow state defaults to the macOS application-support directory. Override
it when isolation is required:

```bash
export FCP_MCP_STATE_DIR=/your/private/state
```

---

## Examples

See the [full gallery](examples/GALLERY.md) for tool sequences and
workflow breakdowns. Sample prompts:

- *"Run the structural QC report on hero.fcpxml and fix every reported flash frame."*
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
   fcp-mcp server (fcp_mcp/server.py, MCP SDK v2)
        │
        ├── fcpxml/parser + writer + models ← rational-arithmetic, defusedxml-hardened
        ├── fcpxml/analysis + validator     ← QC, pacing, flash frames, gaps
        ├── fcpxml/generator + puppet       ← programmatic timeline construction
        ├── fcpxml/diff                     ← A/B timeline comparison
        ├── workflow/                       ← prepare, verify, approve, commit, recover
        ├── private workflow ledger         ← candidates, events, hashes, reconciliation
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
tools require an active FCP process on macOS and
`FCP_MCP_ENABLE_LIVE_CONTROL=1`.

**`fcp_*` tools fail silently on newer FCP versions** — FCP's scripting surface is
limited compared to pre-FCPX versions. Menu items and keyboard shortcuts are the
reliable path; some JXA queries are gated behind Accessibility permissions
(System Settings → Privacy & Security → Accessibility → Claude/Terminal).

**`fcpxml_*` tools report structural validation failures** — run
`fcpxml_validate` for the implemented checks. Passing this validator is
not proof of complete Apple/FCP compatibility. Run
`python scripts/apple_dtd_gate.py path/to/result.fcpxml` on a Mac with
Final Cut Pro installed, then import an important result into a disposable
Final Cut Pro project before relying on it.

**`path_outside_scope`** — add the media location to
`FCP_MCP_ALLOWED_ROOTS`. External volumes are not implicitly trusted.

**`same_file_forbidden`** — choose a different output. fcp-mcp never
overwrites its source path, even when the caller supplies it explicitly.

**`unsupported_contract` from `fcpxml_apply_template`** — template
listing and saving remain available, but clip substitution has no stable
schema and remains intentionally unavailable in v0.3.0.

**Tests failing on import** — activate the venv and reinstall: `pip install -e ".[dev]"`.

---

## Planned Work

See [ROADMAP.md](ROADMAP.md) for the full plan. Highlights:

- **MCP Prompts** — shipped: `qc-check`, `youtube-chapters`, `cleanup`, `rough-cut`, `beat-sync`
- **Proxy/Resolve round-trip** — proxy-aware offline/online workflows

---

## Acknowledgments

fcp-mcp was co-developed by Adam Steen and [Claude](https://claude.ai) (Anthropic).

## License

MIT — see [LICENSE](LICENSE).
