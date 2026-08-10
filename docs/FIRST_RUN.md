# First run: a reviewed FCPXML change

This is the supported first run for fcp-mcp 0.3.0. It produces a local,
reviewed FCPXML artifact; it does not control Final Cut Pro autonomously.
The bundled sample refers to intentionally unavailable media, so its media is
**offline by design**. That keeps this walkthrough reproducible without
private footage.

## 1. Install and check the local runtime

On macOS, install the published package with pipx:

```bash
pipx install fcp-mcp
fcp-mcp --version
fcp-mcp doctor --json
```

`doctor --json` reports the selected profile, allowed roots, output directory,
workflow-ledger health, and optional live-control readiness. Redact local paths
before sharing it; see [Support](../SUPPORT.md).

## 2. Materialize the bundled sample

Choose an existing local directory. The command never overwrites an existing
file and does not create parent directories.

```bash
mkdir -p ~/Movies/fcp-mcp-first-run
fcp-mcp sample --output ~/Movies/fcp-mcp-first-run/first_run.fcpxml
```

The JSON response includes `sample_path` and `next_prompt`. Use its
`sample_path` below; this guide spells it as
`/Users/you/Movies/fcp-mcp-first-run/first_run.fcpxml` only as an example.

## 3. Configure a tested stdio client

The default `workflow` profile exposes 34 tools and preserves the inspect,
prepare, review, hash approval, commit sequence. For Claude Code, install the
following user-scoped stdio configuration:

```bash
claude mcp add-json fcp '{"type":"stdio","command":"fcp-mcp","env":{"FCP_MCP_PROFILE":"workflow"}}' --scope user
```

For Claude Desktop, add the same `fcp` object to
`~/Library/Application Support/Claude/claude_desktop_config.json` under
`mcpServers`, then restart Claude Desktop. Confirm the connection from Claude
Code with `claude mcp list`, or from your client’s MCP-tools view.

Leave `FCP_MCP_ENABLE_LIVE_CONTROL` unset. This walkthrough does not need
Accessibility permission or Final Cut Pro to be running.

## 4. Inspect before changing anything

Ask the connected client to call this tool. The argument is a path inside the
default `~/Movies` allowed root; set `FCP_MCP_ALLOWED_ROOTS` first if you chose
another directory.

```tool-call
{"name":"fcpxml_parse","arguments":{"path":"/Users/you/Movies/fcp-mcp-first-run/first_run.fcpxml"}}
```

Confirm the returned summary identifies the `First Run` project and `Demo Clip`.
The missing media is expected; inspection is of the FCPXML structure.

## 5. Prepare exactly one marker

Prepare a candidate rather than editing the source. The `add_marker` operation
targets `Demo Clip` and writes a separate destination.

```tool-call
{"name":"fcpxml_workflow_prepare","arguments":{"source_path":"/Users/you/Movies/fcp-mcp-first-run/first_run.fcpxml","destination_path":"/Users/you/Movies/fcp-mcp-first-run/first_run_marked.fcpxml","operations":[{"kind":"add_marker","clip_name":"Demo Clip","start":"0s","value":"First-run review marker"}]}}
```

The preview must be `awaiting_approval`. Record and review these evidence
fields before any commit: `run_id`, `source_sha256`, `plan_sha256`,
`candidate_sha256`, `diff_sha256`, `validation`, `operation_receipts`, and
`diff_uri`. Open the semantic diff at `diff_uri`; reject or cancel the run if
the change is not exactly the one marker you asked for.

## 6. Approve the reviewed hash and commit

Only after review, replace the placeholders with the returned `run_id` and the
exact `candidate_sha256`. Supplying the hash is the client approval binding;
do not substitute a hash from a different preview.

```tool-call
{"name":"fcpxml_workflow_commit","arguments":{"run_id":"<RUN_ID_FROM_PREVIEW>","expected_candidate_sha256":"<CANDIDATE_SHA256_FROM_PREVIEW>"}}
```

The receipt must include `run_id`, `candidate_sha256`, `output_sha256`,
`source_sha256`, `destination_path`, `approval_source`, `committed_at`, and
`receipt_sha256`. `output_sha256` must equal the approved candidate hash.

## 7. Validate the committed artifact

```tool-call
{"name":"fcpxml_validate","arguments":{"path":"/Users/you/Movies/fcp-mcp-first-run/first_run_marked.fcpxml"}}
```

Expect `valid: true` and inspect `error_count`, `warning_count`, and `issues`.
For an important handoff, also run Final Cut Pro’s installed DTD gate and import
the result into a disposable library. A successful FCPXML validation or DTD
check is not proof that Final Cut Pro will import every native-library detail.

The committed file is ready for bounded FCPXML interchange and tested handoff;
it is not a request to relink the deliberately offline sample media.
