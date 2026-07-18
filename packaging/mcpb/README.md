# Veqtor for Claude Desktop

Veqtor is a local MCP server for reviewing DOCX redlines with Claude. It reads
Word files only when Claude calls a Veqtor tool with their path. Processing and
the optional `.veqtor` decision-record sidecar stay on this computer; there is
no Veqtor account or hosted document service.

Veqtor supplies bounded document facts and deterministic tracked-change writes.
It does not provide legal advice, establish that filename order is chronology,
or decide whether contract wording is legally or commercially suitable.

## Install

1. Download the versioned macOS `.mcpb` and `SHA256SUMS.txt` from the same
   immutable Veqtor GitHub Release.
2. Check the SHA-256 before opening the bundle.
3. Open the `.mcpb`, review the requested local access and configuration, and
   approve installation in Claude Desktop.
4. Enter the name that should appear as the author of any new Word tracked
   changes. Veqtor does not infer this name from the document.
5. Open Claude Desktop's extension settings and confirm that Veqtor is enabled
   and its local MCP server is connected. If the status does not become
   connected, fully quit Claude Desktop and reopen it before continuing.
6. Run the bundled `try_veqtor_demo` prompt, or paste the prompt from
   `demo/FIRST_PROMPT.txt`.

The first activation may need internet access while Claude Desktop's UV runtime
downloads a compatible Python runtime and the dependencies pinned in
`uv.lock`. Later availability depends on the host cache; this package does not
promise a fully offline first install.

## Bundled demo

The `demo` folder contains four deterministic synthetic DOCX files. They contain
no client data. The server starts with the extension root as its working
directory, so the relative folder name `demo` is a valid Veqtor workspace for
the first read-only prompt.

Read operations normally append a private local `.veqtor` sidecar inside the
workspace. The four bundled DOCX inputs are immutable release assets and the
first prompt is read-only. Do not use the installed `demo` folder for a write
test. Copy the four DOCX files to a fresh writable folder outside the installed
extension, then create any output only inside that copied workspace.

After a demo apply, ask Claude to run `list_rounds` again and re-extract the new
output. Confirm that the original source SHA-256 is unchanged and that the new
file's hashes from `apply_edits`, `list_rounds` and `extract_redlines` agree.

## Update, rollback and uninstall

- Update by downloading and approving a newer versioned `.mcpb`; verify its
  checksum first. Do not assume automatic updates.
- Starting with the release after the first public MCPB, rollback means a
  manual uninstall and reinstall of the previous immutable extension where
  Claude Desktop permits it. The first MCPB has no older public extension to
  restore, so its release gate tests uninstall and same-artifact reinstall
  instead. Veqtor does not promise an in-app rollback mechanism.
- Uninstall from Claude Desktop's extension settings. Remove any output DOCX or
  `.veqtor` sidecar you chose to keep outside the extension separately.

The extension is not an operating-system sandbox. It runs with the permissions
of the current user and can access local paths that are deliberately supplied
to its tools. Review tool calls and outputs before relying on them.

Documentation: https://veqtor.pro/docs

Known limitations: https://veqtor.pro/limitations
