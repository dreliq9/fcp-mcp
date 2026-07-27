# Contributing to fcp-mcp

Thanks for your interest. This guide covers how to add tools, run
tests, and submit changes.

## Setup

```bash
git clone https://github.com/dreliq9/fcp-mcp.git
cd fcp-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"

# Optional but recommended for media_* tool development
brew install ffmpeg
```

## Adding a new tool

Every tool is registered in `src/fcp_mcp/server.py` with an explicit
annotation preset from `tool_metadata.py`. The handler should delegate
policy and side effects to the focused modules under `src/fcp_mcp/`.

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
   - Resolve user paths through the shared `PathPolicy`
   - Parse the input via the helper modules (never inline XML parsing)
   - Return a `str` success result — usually a one-line status plus
     structured JSON. Return `json.dumps(..., indent=2)` for structured
     output
   - Raise `FCPMCPError` with a stable code for known failures; do not
     return failure-shaped success text
   - Commit FCPXML through the transaction layer and other outputs
     through the atomic writer
   - Keep live actions behind `FCP_MCP_ENABLE_LIVE_CONTROL`
   - Pass dynamic AppleScript/JXA values through argv, never source
     interpolation

4. **Classify the tool.** Use one of `OFFLINE_READ`, `OFFLINE_WRITE`,
   `LIVE_READ`, `LIVE_WRITE`, `STATEFUL_WRITE`, or `DIAGNOSTIC`.
   Annotations describe behavior to clients; deterministic code still
   enforces path, live-control, and write policy.

5. **Write a test.** Add a test in the matching file under `tests/`. If
   the tool writes a file, add a fixture under `tests/fixtures/` and
   use `tmp_path` for output. Include the relevant failure path and
   verify no partial output is created. Don't mock `lxml` or
   `defusedxml` — run them for real.

6. **Update executable contracts.** Add the tool to
   `tests/test_tool_catalog.py`, the `CHANGELOG.md`, and the README
   category table. Put every operational Markdown example in a
   `tool-call` fence so `scripts/check_contracts.py` validates it
   against the live input schema.

## Running tests

```bash
source .venv/bin/activate
ruff check src tests scripts
FCP_MCP_PROFILE=full python scripts/check_contracts.py
pytest -v
```

The complete suite runs against real parsers. If a test needs an
optional host dependency, guard it explicitly and keep the deterministic
command-construction and error paths covered everywhere.

## Style

- `ruff` config lives in `pyproject.toml` (line length 100).
- No type annotations in tool signatures beyond the basic JSON-RPC
  types — we need the MCP SDK to infer the schema.
- Prefer `from __future__ import annotations` in modules that import
  from within the package.

## Submitting

1. Fork + feature branch
2. Ruff, documentation contracts, the complete test suite, coverage
   gates, dependency audit, build checks, and installed-wheel smoke must
   pass
3. Open a PR referencing the issue you're addressing; include the
   before/after output of at least one tool invocation in the
   description
4. Keep PRs focused — one tool or one bug per PR where possible

## Release prerequisites

The tag workflow uses PyPI Trusted Publishing. Before creating a `v*` tag,
maintainers must:

1. Register `dreliq9/fcp-mcp`, `.github/workflows/publish.yml`, and the
   `pypi` environment as the trusted publisher for the `fcp-mcp` PyPI
   project.
2. Require a maintainer review on the GitHub `pypi` environment.
3. Protect `v*` tags against unreviewed creation or replacement.

The unprivileged workflow job repeats every release gate and uploads the
verified distributions. Only the separate two-step publish job receives
`id-token: write`. Do not add a long-lived PyPI token to the workflow.

## What not to contribute

- Scripting escape hatches that run arbitrary Python in the server
  process (file an issue instead — we can scope a sandboxed version)
- Tools that duplicate existing functionality with a slightly nicer
  surface — prefer extending the existing tool
- Tools that require paid services without a clear opt-in flag and
  environment variable gate
