# Limitations

Accuracy on public repositories is **not yet measured by a human**. Static name
matching can report intentional examples, third-party commands, dynamic plugin
registrations and scope/index omissions as missing. Read confidence and evidence;
do not file an issue based on an unreviewed result.

All supported frameworks have partial coverage. Django/DRF, yargs, Next.js/NestJS
routes and Go/Rust configuration are not implemented. Function existence does not
prove signatures or behavior. RST support is basic; MDX strips known wrappers
without executing components. Cross-package fallback can be ambiguous.

The scanner bounds file/path/line sizes and nesting. Unsupported files become
warnings. YAML/JSON preflight is conservative and may reject complex valid data.
Shallow history, an 8 MB/12 second patch stream budget, Git errors and unsupported
syntax can prevent attribution; a missing commit is never invented. The history
cap counts traversed commits, not just matching commits.

The recall audit samples doc lines as a proxy for semantic claims and excludes
unverifiable/low-confidence predictions. Its weighted estimate is not exhaustive
recall. Fixture scores are not real-world precision.

Native VS Code UI capture, Linux production-container execution, public hosting,
published package/Action URLs and human accuracy labels remain explicit release
checks. Robustness warning groups and measured performance are recorded in
[results](RESULTS.md); failures must remain visible.

The public-repo sweep uses sparse checkouts. Binary files and Go/Rust paths may
be absent locally, so missing-file findings can reflect acquisition rather than
documentation drift. Syntax errors, minified sources, oversized files and
ambiguous default subjects produce explicit warnings; these are incomplete
coverage, not evidence that the documentation is wrong.

Explicitly generated outputs, example manifest filenames, shorthand and upstream
source paths are retained as unverifiable references rather than required local
files. Local prose context is heuristic: authors should identify external and
illustrative references clearly. Shell examples are checked as static tokens;
positional arguments, option arity and runtime behavior are not proved. Tokens
after the shell end-of-options marker are positional data.

Custom URI schemes, scoped package names and import aliases are not required
checkout filenames. Slash-separated names without a filename extension or
explicit file/directory context require positive filesystem evidence; otherwise
they remain unverifiable. This avoids treating protocol messages and media types
as missing files. Creation instructions describe output files rather than files
that must already exist. Unsupported alias resolution and plans still require
review; these heuristics do not establish general precision or recall.

TSX currently uses the TypeScript grammar. Valid JSX can therefore trigger syntax
recovery warnings; recognizable text declarations are retained. Warning counts
must not be described as counts of syntax defects in the scanned projects.
Byte-identical output was verified on all fixtures and pinned Flask. This does
not prove determinism for every repository: history that hits a wall-time budget
can expose different amounts of attribution on different runs or hosts.

Performance targets currently fail. The large Next.js run stayed below the
traced Python allocation budget, which does not measure whole-process memory.
Repeated package lookup and repository file reads dominate its profile. On this
Windows environment, one timed-out sweep process also needed manual process-tree
cleanup after the normal cleanup stalled. A requested timeout is not a proven
hard wall-clock bound on every Windows host.
