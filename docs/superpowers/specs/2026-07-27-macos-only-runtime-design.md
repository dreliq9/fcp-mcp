# fcp-mcp macOS-Only Runtime Design

**Date:** 2026-07-27
**Status:** Design direction approved; written review requested
**Amends:** [`2026-07-26-v0.3.0-transactional-workflows-design.md`](2026-07-26-v0.3.0-transactional-workflows-design.md)

## Decision

`fcp-mcp` is a macOS-only product runtime.

Windows and Linux are not supported, tested, documented, or maintained as
operational targets. The package remains import-tolerant: importing pure
modules for metadata inspection, documentation, type analysis, or other static
tooling does not perform a platform check. This is not a portability promise.
Code that happens to work outside macOS is incidental.

The distinction is deliberate:

- imports describe and inspect code;
- runtime initialization performs work and establishes the support boundary.

No engineering effort will be spent preserving Windows or Linux behavior.

## Research basis

Apple distributes Final Cut Pro for Mac and iPad, not Windows or Linux. The
current Mac release requires macOS:

- [Final Cut Pro product and requirements](https://www.apple.com/final-cut-pro/)
- [Final Cut Pro technical specifications](https://support.apple.com/111903)

Apple identifies the current user's Application Support directory as the
standard location for app-managed support data:

- [Foundation `applicationSupportDirectory`](https://developer.apple.com/documentation/foundation/url/applicationsupportdirectory)
- [macOS Library directory details](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/MacOSXDirectories/MacOSXDirectories.html)

Python package operating-system classifiers communicate support but do not
provide a runtime enforcement boundary. `fcp-mcp` therefore uses both truthful
Mac-only metadata and an explicit operational guard:

- [Python core metadata specification](https://packaging.python.org/en/latest/specifications/core-metadata/)

## Runtime boundary

Create one side-effect-free platform policy function. It returns normally only
on Darwin and otherwise raises the stable domain error
`unsupported_platform`.

The guard is required at every public operational boundary:

1. no-argument `fcp-mcp` startup;
2. `fcp-mcp serve`;
3. `fcp-mcp doctor`;
4. direct `fcp_mcp.server.main()` invocation;
5. workflow runtime initialization and any future alternate executable entry
   point.

The command-line boundary writes one bounded, path-free error to stderr and
returns exit code `2`. A direct Python operational boundary raises the coded
domain error. No unsupported-platform path may:

- read runtime configuration;
- create a state directory or file;
- initialize SQLite;
- construct or expose an MCP tool catalog;
- launch or probe Final Cut Pro;
- mutate any caller file.

`--help` and `--version` remain available on every platform because they do not
initialize the product runtime.

Importing `fcp_mcp`, FCPXML data models, parsers, or other pure modules does not
run the guard. Public operational APIs must not rely on import-time side
effects for enforcement.

## macOS state and filesystem contract

The default state root remains the per-user macOS Application Support location
resolved by `platformdirs`, with `FCP_MCP_STATE_DIR` as the explicit override.
Using `platformdirs` here is a macOS directory-resolution choice, not a
cross-platform commitment.

The runtime continues to require:

- private state directories and files;
- symlink-safe, descriptor-relative traversal;
- atomic publication and replacement;
- durable commit intent and recovery evidence;
- sanitized filesystem and SQLite failures.

Delete Windows-only reparse-point, Win32 handle, mutex, liveness, and
share-mode implementations. Keep only the macOS filesystem implementation and
tests. Generic names such as `POSIX` may be narrowed to `macOS` where they
otherwise imply a supported Linux contract.

## Packaging, documentation, and release

Remove:

- the `Operating System :: OS Independent` classifier;
- Windows/Linux parity statements;
- Windows path-separator instructions;
- Windows/Linux setup or support claims;
- non-macOS runtime matrices and installed-wheel smoke tests.

Keep only the Mac operating-system classifier. A pure-Python wheel may remain
tagged `py3-none-any`; that tag describes wheel contents, not supported product
runtime. The runtime guard is authoritative.

Run tests, Ruff, contract validation, dependency audit, coverage, package
build, installed-wheel smoke, and publication on macOS runners. Exercise
Python 3.10 through 3.13 on macOS.

Release documentation must state:

- macOS is required;
- Final Cut Pro is required only for live-control and final import-canary
  operations;
- offline FCPXML operations are still macOS-only as a supported product
  boundary.

## Transactional-workflow plan impact

The platform change does not weaken the transactional workflow invariants.
Keep Task 14 repairs for:

- complete prepare evidence before approval;
- approval-mode/source consistency;
- final commit receipt evidence;
- exact commit-intent and commit-result projections;
- terminal error consistency;
- closed terminal audit events and resumable prune intents;
- non-mutating reads;
- sanitized cleanup failures;
- authenticated SQLite migration backups on macOS.

Discard uncommitted Windows-specific ledger implementation and tests. Amend
later tasks to remove Windows lock-owner liveness and cross-platform filesystem
requirements. All acceptance and release gates run against macOS.

## Test strategy

Write the platform-policy tests before implementation:

1. simulate a non-Darwin platform and verify every operational entry point
   fails with `unsupported_platform`;
2. prove failure occurs before configuration, state, SQLite, server catalog,
   subprocess, or filesystem activity;
3. verify `--help`, `--version`, and pure imports remain side-effect-free;
4. verify Darwin passes the guard;
5. run the complete suite and installed-wheel smoke on macOS.

Platform simulations test the rejection boundary only. They do not constitute
Windows or Linux compatibility testing.

## Acceptance criteria

The amendment is complete when:

- no supported-runtime claim names Windows or Linux;
- all operational entry points reject non-Darwin before side effects;
- Windows-specific production code and tests are removed;
- CI and publication use macOS runners only;
- package metadata names only macOS;
- Task 14 retains and passes every platform-independent acceptance repair;
- the complete macOS suite, lint, contracts, coverage, audit, build, and wheel
  smoke gates pass;
- a maintainer-authorized Final Cut Pro import canary remains the final release
  proof.

## Non-goals

- Making package installation fail outside macOS.
- Adding import-time platform side effects.
- Preserving accidental non-macOS behavior.
- Supporting Final Cut Pro virtualization, remote GUI forwarding, or
  compatibility layers.
- Weakening file, approval, recovery, or artifact integrity because only one
  operating system is supported.
