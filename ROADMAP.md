# fcp-mcp Roadmap

Status as of 2026-04-23: **88 tools across 12 categories.** First
public release. Planned work below, grouped by priority.

---

## v0.2 — Prompts + CI

**Goal:** reduce boilerplate for common agent flows and guarantee green
builds on every change.

### 0.2.1 MCP Prompts (5)

Pre-built prompts that wrap the most common tool sequences. The client
sees them in the MCP prompt list and can invoke them by name.

- **`qc-check`** — `fcpxml_qc_report` + interpretation guide for the LLM
- **`rough-cut`** — `fcpxml_auto_rough_cut` with beat-sync scaffold
- **`cleanup`** — `fix_flash_frames` + `fill_gaps` + re-QC
- **`youtube-chapters`** — extract markers, emit YouTube timestamp blob
- **`beat-sync`** — `media_detect_beats` + `auto_rough_cut` at beats

### 0.2.2 GitHub Actions CI

- Test matrix: Python 3.10 / 3.11 / 3.12 / 3.13
- `pytest -v` gate on every PR
- `ruff check` gate on every PR
- Coverage badge via `pytest-cov`

### 0.2.3 Release automation

- Tag-triggered workflow → build wheel + sdist → publish to PyPI
- Post-publish hook → submit to MCP Registry via `mcp-registry` tool

---

## v0.3 — Production recipes

**Goal:** match DareDev256's WORKFLOWS.md depth with a fcp-mcp-specific
catalog.

- `WORKFLOWS.md` — 8+ production recipes with full tool sequences:
  - Wedding/event highlight reel
  - Long-form interview cleanup (silence + fillers)
  - Multi-cam sync and proxy round-trip
  - Podcast-to-YouTube conversion
  - Motion-graphics-heavy promo with puppet system
  - Color round-trip through DaVinci Resolve
  - Premiere Pro handoff (FCP7 XMEML)
  - Compressor-dispatched batch encode matrix

---

## v0.4 — Proxy + offline media awareness

**Goal:** make offline/online workflows first-class.

- `fcpxml_detect_proxies(path)` — flag clips with proxy references but
  no original
- `fcpxml_relink_media(path, search_root)` — re-resolve offline clips
  against a new media root (matches by basename + duration)
- `fcpxml_make_offline(path, pattern)` / `fcpxml_make_online(...)` —
  toggle clips between online and offline metadata
- Integration with `fcp_import_xml` — offline clips get a warning in
  the return payload instead of silently dropping

---

## v0.5 — Sandbox + hardening

**Goal:** safe to run alongside untrusted clients.

- Sandbox the live FCP tools behind an allowlist (opt-in via env var)
- File-system jail for all `output_path` parameters to a configurable
  root
- Rate-limit live FCP tools (AppleScript is slow; 1 call per 500ms
  naturally, but add an explicit cap)
- Security posture doc — XXE, billion-laughs, path traversal test
  matrix documented + `tests/test_security.py` added to the public
  test surface

---

## v1.0 — Stability pact

**Goal:** tool names + signatures frozen. Additions only, no renames.

- Freeze the 88 v0.x tools after one full major-version shakedown
- Commit to 12-month deprecation window for any future removals
- Version the MCP Prompts too — existing prompt names become stable
- Publish `docs/STABILITY.md` stating what's covered by the pact

---

## Not planned

- **Resolve Fusion / Premiere Motion templates** — out of scope; we
  emit FCPXML + Resolve XML + FCP7 XMEML, not compositor-specific formats
- **Cloud-hosted endpoint** — MCP is local-stdio by design; hosted
  variants belong in downstream products, not this repo
- **Desktop GUI** — the MCP protocol is the UI
