# Contributing to fcp-mcp

Thanks for your interest. This guide covers how to add tools, run
tests, and submit changes.

## Setup

```bash
git clone https://github.com/dreliq9/fcp-mcp.git
cd fcp-mcp

python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows (FCPXML tools only — live FCP tools are macOS)

pip install -e ".[dev]"

# Optional but recommended for media_* tool development
brew install ffmpeg             # or apt install ffmpeg on Linux
```

## Adding a new tool

Every tool is defined in `src/fcp_mcp/server.py` with the FastMCP
`@mcp.tool()` decorator. The tool body is thin — it calls out to
a helper module under `src/fcp_mcp/`.

1. **Pick the right module.** If the tool extends an existing category,
   extend the existing module (`fcpxml/analysis.py`,
   `fcpxml/generator.py`, etc.). New categories get new modules.

2. **Follow the naming convention.**
   - FCPXML-only operations: `fcpxml_<verb>_<noun>` (e.g.,
     `fcpxml_remove_silence`)
   - Live FCP operations: `fcp_<verb>` (e.g., `fcp_playback`)
   - Media analysis: `media_<verb>` (e.g., `media_detect_beats`)
   - Puppet rigs: `puppet_<verb>` (e.g., `puppet_animate`)
   - Compressor dispatch: `compressor_<verb>`

3. **Write the handler.** The handler in `server.py` should:
   - Accept string/int/float/bool/JSON-string parameters (the MCP surface
     is JSON-RPC — don't take complex Python types)
   - Parse the input via the helper modules (never inline XML parsing)
   - Return a `str` result — usually a one-line OK/FAIL status plus
     structured JSON. Return `json.dumps(..., indent=2)` for structured
     output
   - Guard destructive operations behind a validator call

4. **Write a test.** Add a test in the matching file under `tests/`. If
   the tool writes a file, add a fixture under `tests/fixtures/` and
   use `tmp_path` for output. Don't mock `lxml` or `defusedxml` — run
   them for real.

5. **Update docs.** Add the tool to the `README.md` category table, the
   `CHANGELOG.md` under the next version, and (if its invocation is
   non-obvious to an LLM) a note in `LLM_GUIDE.md`.

## Running tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

Tests run against real parsers — no mock XML, no fake ffmpeg. If a test
needs ffmpeg, guard it with `@pytest.mark.skipif(not shutil.which("ffmpeg"))`.

## Style

- `ruff` config lives in `pyproject.toml` (line length 100).
- No type annotations in tool signatures beyond the basic JSON-RPC
  types — we need the MCP SDK to infer the schema.
- Prefer `from __future__ import annotations` in modules that import
  from within the package.

## Submitting

1. Fork + feature branch
2. `pytest` must pass (107+ tests)
3. Open a PR referencing the issue you're addressing; include the
   before/after output of at least one tool invocation in the
   description
4. Keep PRs focused — one tool or one bug per PR where possible

## What not to contribute

- Scripting escape hatches that run arbitrary Python in the server
  process (file an issue instead — we can scope a sandboxed version)
- Tools that duplicate existing functionality with a slightly nicer
  surface — prefer extending the existing tool
- Tools that require paid services without a clear opt-in flag and
  environment variable gate
