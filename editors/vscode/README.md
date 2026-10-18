# Docs Drift VS Code client

Local Markdown/MDX/RST diagnostics from the docs-drift language server. Static
source inspection; no target code execution. This extension is distributed as a
VSIX release attachment, not through the Marketplace.

Install the CLI with its lsp extra in an environment available to VS Code, then
use **Extensions → Install from VSIX**. Set docsDrift.executable to the absolute
path of that environment's docs-drift executable. On the synthetic demo, open
the Parcel repository root and its README. Change --port to --listen and the
documented worker default from 4 to 8; do not change the source.

Source and limitations: https://github.com/ayushagarwalhere/docs-drift.
