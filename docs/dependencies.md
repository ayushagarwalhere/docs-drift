# Dependency drift

Code changes faster than installation docs. A project raises its minimum Python,
drops a library, or renames an install extra, and the README keeps telling people
the old story. docs-drift checks that story in two ways:

1. **Docs vs manifests.** Runtime minimums and install commands in your docs are
   checked against the package manifests. This runs in every `scan` and `check`
   and is controlled by `[deps] enabled`.
2. **Code vs manifests.** Imports in your source are compared with the
   dependencies you declare, which shows packages that are used but not declared,
   and packages that are declared but never imported. This runs in
   `docs-drift deps`. To include it in normal scans, set `usage` to true in the deps table.

Both parts read files as data only. No package manager, build backend, setup.py
or package script runs, and no registry is contacted. The current version on PyPI
or npm is never looked up.

## What is read

| Source | Fields |
| --- | --- |
| `pyproject.toml` | `[project]` name, version, `requires-python`, dependencies, optional dependencies; `[dependency-groups]`; `[build-system]` requires; Poetry, uv and PDM dependency tables |
| `setup.cfg` / `setup.py` | name, `python_requires`, `install_requires`, `extras_require` (literal values only; `setup.py` is parsed, never executed; with a `**options` expansion the extras count as unknown) |
| `requirements*.txt`, `requirements/*.txt`, `Pipfile` | requirements and nested `-r` includes inside the repository; file names containing dev/test/lint/docs count as development dependencies; `constraints*.txt` and files named by `-c` are version pins only, never runtime dependencies |
| `package.json` | name, version, `private`, `engines.node`, `dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies`, scripts |
| `.python-version`, `.nvmrc`, `.node-version`, `tox.ini` `envlist` | weak runtime evidence, used only when no declared range exists |

Each documentation file is checked against the nearest manifest in its directory
or a parent directory, so package READMEs in a monorepo use their own manifest.

## Documentation checks

| Documentation says | Checked against | Verdict when it disagrees |
| --- | --- | --- |
| A runtime minimum in prose, a table row or a shields.io badge | `requires-python`, `python_requires`, Poetry `python`, `engines.node` | `version_mismatch` (DD006) |
| An install command with extras for this project | optional-dependency names | `unknown_extra` (DD007) |
| An install command pinning a declared dependency | the declared version range | `version_mismatch` (DD006) |
| An install command pinning this project's own version | the manifest version: only a pin newer than it (an unreleased version) is reported, and never while the manifest carries a dev, pre-release or local version | `version_mismatch` (DD006), lower confidence |
| An install command whose other packages are all declared dependencies | whether the one remaining package is still declared | `missing` (DD001) |

A runtime minimum is compared with the oldest release the manifest admits, at the
precision the docs use: a documented Python minimum of "3.8" matches `>=3.8.1`,
and a suggested fix uses the real bound (3.8.1), never the next minor release. If
the docs claim support for an older release than the manifest allows, that is drift.
If the docs ask for a newer release than needed, the claim is reported as `ok`
with a note, because the docs are stricter rather than wrong. Set
`report_understated = true` under `[deps]` to report that case as
`version_mismatch` with confidence 0.6.

Runtime statements need an explicit lower-bound wording: a trailing `+`, `>=`,
"or later", "or newer", "requires", "at least", or a range. Version numbers that
only describe an environment, such as a benchmark machine, are not claims. Python
claims need a minor version, because "Python 3" does not say enough to check.
"Node.js" and "NodeJS" always name the runtime. A bare "Node" is also a graph or
cluster word ("Node 5 and up are leaves"), so it counts as Node.js only with an
explicit marker (`node >= 16`, `Node v16`, "Node version 16") or in runtime
context: npm, Yarn, pnpm, `engines`, install, requirements or compatibility
wording on the same line, or a section heading or table header such as
Installation, Requirements or Runtime. "requires node 2" is never a claim, and
majors 1 to 3 are not Node.js releases.

A minimum that names a release of the project describes that release. This can be a
table cell (`2.x`, `1.x (legacy)`, `2.4 LTS`, `1.0 - 1.9`, `<2.0`, any cell that
starts with a release and adds a qualifier, such as `2.x - maintained until 2027`,
any cell under a Version, Release, Series or Branch column, or under a column named
after the project) or a line that names the release, for example:

- `2.x: Python 3.7+`
- `1.x supports Python 2.7+`, also with the project name in front (`demo 1.x`)
- `Releases before 2.0 supported Python 2.7+`

Such a minimum is checked only when the release covers the version in the manifest. Otherwise it
is compatibility history, and it is never reported or rewritten. It is also
skipped when the manifest version is not a literal. A row labelled `latest`,
`current` or `main` describes the current release and is checked. When one section
lists several minimums for the same runtime, only the newest of the remaining ones
is checked.

Install commands are recognised in shell code blocks and in inline code. The
supported tools are pip, `python -m pip`, uv (`uv pip install`, `uv add`,
`uv tool install`, `uvx --from`), pipx, Poetry, PDM, npm, pnpm, Yarn and Bun.
Requirement files, local paths, URLs and VCS references are skipped. A package
that the manifests don't mention is reported only when every other package in
the same command is a declared dependency; installing this project next to an
example library, or several undeclared libraries together, is a tutorial. A one-off tool install, such as
pre-commit, is never reported. Global installs and packaging tools such as pip,
setuptools and wheel are also ignored.

Claims that the manifests can't decide are dropped instead of being reported as
unverifiable. Historical sections, changelogs, suppression comments and
`ignore` patterns such as `dependency:requests*` or `runtime_version:*` work the
same way as for every other claim.

`docs-drift fix` can rewrite a stale runtime minimum outside tables, and an
extra name that has a close match. It never rewrites dependency pins, so a person decides those.

## Code checks

`docs-drift deps` parses Python with the standard `ast` module, and parses
JavaScript and TypeScript with the bundled tree-sitter grammars. It reports
these issue kinds:

| Kind | Meaning | Default confidence |
| --- | --- | --- |
| `undeclared` | Runtime code imports a package that no manifest declares | 0.85 Python (0.55 if only imported inside functions), 0.8 Node |
| `dev_only` | Runtime code imports a package declared only for development | 0.65 |
| `undeclared_dev` | Tests, tooling or type-checking code imports an undeclared package | 0.55 |
| `unused` | A runtime dependency that no source file imports and no script or configuration names | 0.7 Python, 0.65 Node |
| `added_undocumented` | With `--base`: a new runtime dependency that the docs never name, although they name other dependencies | 0.6 |
| `removed_documented` | With `--base`: a removed dependency that the docs still name in code | 0.65 |

To keep false positives low:

- Standard-library modules come from `sys.stdlib_module_names` plus modules that
  newer Python versions removed. Node built-ins, `node:` imports and modules the
  host provides (`vscode`) are never reported.
- Local modules, relative imports, workspace package names, tsconfig.json path
  aliases and `baseUrl`-style imports of local folders are skipped.
- Import names are mapped to distribution names for well-known mismatches
  (`PIL` to pillow, `yaml` to pyyaml, `bs4` to beautifulsoup4, `sklearn` to
  scikit-learn, `cv2` to the OpenCV wheels, namespace packages such as
  `google.cloud.storage`). Add your own mappings under `[deps] import_map`.
- Imports inside a `try` block that catches ImportError (including its fallback
  and `else` branches) are treated as optional and are not reported. So are
  imports under feature, version or platform tests, such as `if HAS_SIMPLEJSON:`
  or `if sys.version_info < (3, 8):`. Type-checker-only modules such as
  `_typeshed` are skipped.
- Imports under `if TYPE_CHECKING:` and TypeScript `import type` count as
  development use. A type-only import is satisfied by a matching `@types/` package.
- Files under `tests/`, `scripts/`, `docs/`, `examples/` and similar folders,
  `test_*.py`, conftest.py, `*.test.ts`, `*.config.js`, gulp and grunt files and
  dotfiles count as development code.
- Requirements files next to a package manifest that declares runtime
  dependencies are treated as development or CI pins. Files generated by
  pip-compile (with `# via` comments or hashes) are treated as transitive pins,
  not direct dependencies. Requirements files in docs, test or CI folders count
  as development dependencies.
- Packages that are normally used without an import are never reported as unused.
  This includes servers, stub packages, pytest and flake8 plugins, build backends,
  `typescript`, `tslib` and `@types/*`. The same applies to a package named in a
  package script, Makefile, Dockerfile, Procfile, CI YAML or other configuration
  file. Lock files and manifests don't count as references.
- Private Node apps (`"private": true`) are not checked for runtime imports of
  development dependencies, because bundled apps often keep everything in
  `devDependencies`.

Analysis stops after `max_files` source files (5000 by default) and records a
warning. In that case unused-dependency results are left out, because part of
the source was not read.

## Command

```text
docs-drift deps                          # docs vs manifests, and code vs manifests
docs-drift deps --all                    # include OK claims and low-confidence issues
docs-drift deps --format json            # stable JSON contract (schema_version 1)
docs-drift deps --format markdown        # for PR comments
docs-drift deps --check                  # exit 1 when qualifying drift or issues remain
docs-drift deps --base origin/main       # only drift and issues the branch introduced
docs-drift deps --write-baseline deps-baseline.json
docs-drift deps --baseline deps-baseline.json --check
```

Findings and issues below `min_confidence` are hidden unless you pass `--all`.
The global options `--repo`, `--config`, `--output` and `--quiet` work as they
do for every other command.

`--base` makes a static archive of the base revision, scans it the same way, and
keeps only documentation drift and code issues that are new in the current
checkout. It also compares runtime dependencies between the two revisions. A new
dependency that the docs never name is reported as `added_undocumented`, and a
removed dependency that the docs still name is reported as `removed_documented`.
Fetch the base revision first, as you would for `docs-drift pr`.

Documentation findings are ordinary findings, so `docs-drift check --baseline`,
`docs-drift baseline update`, `docs-drift pr` and `docs-drift impact` include them
with no extra setup. The dependency baseline only covers the `deps` command. It
stores claim IDs with verdicts and stable issue IDs.

## Configuration

```toml
# .docs-drift.toml
[deps]
enabled = true              # documentation checks in scan/check (default on)
usage = false               # also run code-vs-manifest analysis in scan/check
report_understated = false  # report docs that ask for a newer runtime than needed
ignore = ["pywin32", "@internal/*"]   # distribution or package globs
import_map = { mymod = "my-distribution" }
max_files = 5000
```

With `usage = true`, scan JSON gains a `dependency_issues` array whenever the
analysis finds issues; the key is left out when there are none. The terminal
and Markdown reports add a one-line summary, but `check` does not fail on these
issues. Use `docs-drift deps --check` to gate on them.

## Limitations

- Versions are compared without a registry, so `latest`, dist-tags, `workspace:`,
  `file:`, `git` and URL ranges are not compared.
- CI matrices other than the tox `envlist` are not read yet.
- Imports built at runtime, such as `importlib.import_module(name)` or `require`
  with a computed string, are not seen. A package used only that way can be
  reported as unused; list it in `ignore` or `import_map`.
- A Python file belongs to the nearest Python manifest. A repository whose only
  Python manifest sits in a subfolder is not checked for imports outside it.
