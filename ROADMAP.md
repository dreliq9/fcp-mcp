# fcp-mcp Roadmap

Status as of 2026-07-26: **89 tools across 12 functional categories
plus runtime diagnostics, and five prompts.** v0.2.1 is the trust
baseline release candidate. Planned work below is grouped by priority.

---

## v0.2 — Prompts + trust baseline

**Goal:** reduce boilerplate for common agent flows and establish a
reproducible, bounded local execution plane.

### 0.2.0 MCP Prompts (shipped)

Pre-built prompts that wrap the most common tool sequences. The client
sees them in the MCP prompt list and can invoke them by name.

- **`qc-check`** — `fcpxml_qc_report` + interpretation guide for the LLM
- **`rough-cut`** — `fcpxml_auto_rough_cut` plus structural QC
- **`cleanup`** — `fix_flash_frames` + `fill_gaps` + re-QC
- **`youtube-chapters`** — extract markers, emit YouTube timestamp blob
- **`beat-sync`** — `media_detect_beats` + `auto_rough_cut` using median
  beat cadence; exact beat cut points are not supported

### 0.2.1 Trust baseline (release candidate)

- Stable coded MCP failures and structured `fcp_doctor`
- Allowed-root path policy and symlink containment
- Validated atomic FCPXML writes with backups and rollback
- Opt-in live FCP, Accessibility, and Compressor actions
- AppleScript/JXA argv isolation
- Explicit MCP annotations on all 89 tools
- Executable documentation and prompt contracts
- Python 3.10–3.13 CI, offline Windows coverage, dependency audit,
  coverage floors, package checks, and installed-wheel smoke
- FastMCP v1 bounded below v2, with package and wire versions disclosed
  separately

### Release automation

- Tag-triggered workflow rebuilds and verifies the candidate, then hands its
  artifact to an isolated PyPI Trusted Publishing job
- The PyPI environment, required reviewers, trusted-publisher registration,
  and protected `v*` tags must be configured before release
- Tags, PyPI upload, GitHub Releases, and MCP Registry submission remain
  separate maintainer-authorized actions

---

## v0.3 — Transactional workflows

**Goal:** make multi-step FCPXML changes previewable, approvable,
crash-recoverable, and machine-verifiable.

- Default `workflow` capability profile with explicit `inspect`, `edit`, and
  `full` alternatives
- Meaningful typed structured outputs while preserving successful text content
- Deterministic inspect-plan-dry-run-validate-diff-approve-commit graph
- Durable SQLite event ledger, private hashed artifacts, and recovery tooling
- CLI approval by default with explicit client-approval opt-in
- Stable MCP SDK v2 migration after the final SDK/specification research gate

---

## v0.4 — Production recipes

**Goal:** rebuild the fcp-mcp-specific recipe catalog on the transactional
workflow surface.

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

## v0.5 — Proxy + offline media awareness

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

## v0.6 — Further policy hardening

**Goal:** safe to run alongside untrusted clients.

- Extend the v0.3 capability profiles with per-tool path and execution policies
- Rate-limit live FCP tools (AppleScript is slow; 1 call per 500ms
  naturally, but add an explicit cap)
- Publish a consolidated security posture and adversarial trajectory
  matrix

---

## v1.0 — Stability pact

**Goal:** tool names + signatures frozen. Additions only, no renames.

- Freeze the shipped public tool set after one full major-version shakedown
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
