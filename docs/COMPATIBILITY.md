# Compatibility

fcp-mcp 0.3.0 is a macOS-only FCPXML workflow product. It produces bounded
FCPXML interchange artifacts and a tested Final Cut Pro handoff; it does not
promise autonomous Final Cut Pro control or complete native-library mutation.

## Tested matrix

| Surface | Tested release | Contract |
|---|---|---|
| Operating system | macOS 15.6 | macOS is required; other operating systems exit as unsupported. |
| Final Cut Pro | Final Cut Pro 12.2 | Used for the tested import handoff; it is not required for offline FCPXML work. |
| FCPXML | FCPXML 1.11 | The bundled first-run artifact and release handoff use 1.11. |
| Python | Python 3.10–3.13 | Supported interpreter range for the 0.3.0 package, from Python 3.10 through Python 3.13. |
| MCP client | Claude Code and Claude Desktop over stdio | Use the `fcp-mcp` stdio command and select a profile through `FCP_MCP_PROFILE`. |

The tested Claude Code configuration is:

```bash
claude mcp add-json fcp '{"type":"stdio","command":"fcp-mcp","env":{"FCP_MCP_PROFILE":"workflow"}}' --scope user
```

Claude Desktop uses the same server object under `mcpServers` in
`~/Library/Application Support/Claude/claude_desktop_config.json`. Other
standards-compatible stdio clients may work, but are not part of this tested
client matrix.

## Profiles and authority

| Profile | Tools | Intended boundary |
|---|---:|---|
| `inspect` | 30 | Offline inspection and diagnostics only. |
| `workflow` (default) | 34 | Inspect, private prepare, semantic-diff review, hash approval, then commit. |
| `edit` | 75 | Direct offline mutation plus workflow tools. |
| `full` | 94 | Offline, media, and opt-in live-control catalog. The `youtube-mcp.materialized-clip-plan/v1` adapter is edit/full only. |

`FCP_MCP_ENABLE_LIVE_CONTROL=1` is a separate opt-in gate. Selecting `full`
does not enable live control, and no profile makes Final Cut Pro automation
autonomous. Leave it unset for the default offline workflow.

## Permissions and local paths

The default output and allowed root is `~/Movies`. To work elsewhere, make the
root explicit before starting the server:

```bash
export FCP_MCP_ALLOWED_ROOTS="/Users/me/Movies:/Volumes/Media"
```

Input paths outside `FCP_MCP_ALLOWED_ROOTS`, including paths reached through
symlinks, are rejected. Offline FCPXML work needs normal read/write permission
to the chosen directories. Opt-in live Final Cut Pro or Compressor actions also
need macOS Accessibility permission for the invoking client or terminal, and
may require Final Cut Pro or Compressor to be installed and running.

## FCPXML and import boundary

`fcpxml_validate` reports fcp-mcp’s implemented structural checks. A DTD check
tests the FCPXML document grammar; neither a DTD result nor this validator is a
guarantee that Final Cut Pro will import the file without warnings or preserve
every native-library detail. Import important output into a disposable Final
Cut Pro library and inspect the result before using it in production.

The supported promise is FCPXML 1.11 interchange and the tested handoff above.
It does not include generic native-library mutation, template substitution,
proxy relinking, media copying, multicam, transcription, or unverified live UI
behaviors. `fcpxml_apply_template` intentionally returns
`unsupported_contract` in 0.3.0.
