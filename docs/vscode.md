# VS Code

The development extension is in `editors/vscode`. Build/package instructions are
in that folder. It launches `docs-drift lsp` over stdio and needs the scanner with
the `lsp` extra installed. The server publishes diagnostics for unsaved document
text, safe exact-span quick fixes, and source definitions. Multi-root ownership
and stale-file checks are supported; dynamic framework coverage remains partial.

**Actual VS Code squiggle GIF: pending a verified editor recording.** The LSP
transport is tested, but an editor animation must not be fabricated from that
test. The earlier native-app capture was unavailable. Record an actual stale
fixture diagnostic, apply its quick fix, and verify the squiggle disappears before
adding the final GIF to the README.
