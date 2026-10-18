"""Build reviewable public documentation drafts without inventing releases, labels or voice usage."""

from pathlib import Path

from docs_drift.support import support_table
from scripts.generate_reference import render_reference

PAGES = {
    "getting-started.md": """# Getting started

This is an unreleased development checkout. PyPI publication and a public demo
URL are pending; do not assume a package or hosted link has already been published.

```sh
uv sync --locked --all-extras
uv run docs-drift scan --repo . --no-color
```

`scan` exits 0 after producing a report, even when drift is found. `check` exits
1 for findings at the configured confidence threshold and 2 for usage/configuration
errors. It never imports target modules, installs target dependencies, or runs
target documentation commands. Install Git for attribution.

After publication, use `pipx install docs-drift` or `uvx docs-drift scan`.
The local web demo uses `uv run python -m docs_drift.web`; see [deployment](deploy.md).
""",
    "claims.md": """# Claim kinds and verdicts

Claims include CLI flags/commands, environment variables, HTTP endpoints,
literal settings/defaults, code symbols, file paths, package scripts, task targets
and project-owned shell command examples. Arbitrary behavioural prose is not
proved by static name matching. The optional model path is separately labeled.

| Verdict | Meaning | Rule |
| --- | --- | --- |
| missing | No supported declaration/path was found; inspect confidence and scope | DD001 |
| renamed | A replacement is proposed from similarity or history evidence | DD002 |
| default_changed | An explicit documented default disagrees with a declaration | DD003 |
| invalid_command | A project-owned command has unknown tokens | DD004 |
| unverifiable | Available static evidence cannot decide | DD005 |
| ok | The extracted claim has a matching static declaration | DD000 |

An `ok` result does not prove runtime success. Low-confidence missing findings are
diagnostics, not verified defects. Historical sections/changelogs are excluded.
Use [the benchmark](benchmark.md) for human-reviewed accuracy, not fixture scores.
""",
    "github-action.md": """# GitHub Action

The repository includes a composite Action in `action.yml`. It is a development
Action, not a published Marketplace release. The action installs its own trusted
source in an isolated environment and parses the checked-out target as data.
It currently supports Ubuntu/Linux runners, not Windows composite execution.

```yaml
name: Documentation
on: [push, pull_request]
permissions: {contents: read}
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803
      - uses: ./
        with: {repo: '.', fail-on-drift: 'true'}
```

This local-action snippet applies to this source repository. For another project,
replace `./` with the real action repository pinned to a reviewed commit SHA after
publication. Inputs are passed through environment variables, never interpolated
into shell source. Reports go to a runner temporary directory: HTML, JSON, SARIF,
and Markdown, plus the Actions job summary. Output: `report-directory`.

The Action never posts PR comments, accepts model/API keys, or calls a model.
It uses no write token. Do not use `pull_request_target` to check out PR code with
secrets/write permissions. Fork PR checks need only read permissions. JSON/HTML
and Markdown are redacted at serialization; repository caches are not trusted.

![Generated PR report preview, not a submitted GitHub comment](assets/pr-comment-preview.png)

This image is a preview of the generated Markdown, not proof of a posted comment.
""",
    "ci.md": """# Pre-commit and other CI

`docs-drift init` offers a local pre-commit hook and workflow. Review generated
files before enabling them; existing files are preserved. Install the scanner
in the hook environment, then run `pre-commit install`.

```yaml
repos:
  - repo: local
    hooks:
      - id: docs-drift
        name: Check documentation drift
        entry: docs-drift check
        language: system
        pass_filenames: false
```

Use `check` in any CI that provides Python and Git. Full history improves
attribution; shallow/truncated history is reported. No write token is required.
Baselines are available for incremental adoption, but are not evidence that old
findings were correct. This project's own docs check must not use a baseline to
hide undocumented behavior.
""",
    "vscode.md": """# VS Code

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
""",
    "python-api.md": """# Python API

```python
from pathlib import Path
from docs_drift.config import Config
from docs_drift.scanning import run_scan
from docs_drift.report import render

result = run_scan(Path("."), Config())
json_report = render(result, "json")
html_report = render(result, "html")
```

`run_scan` returns the versioned Pydantic `ScanResult`. The caller decides whether
to write it. `result.timings` contains measured stage seconds; canonical JSON
omits nondeterministic timing values. Set `history=False` only when intentionally
running a static-only diagnostic. The public benchmark enables history.

Pass `docs_repo` for a separate documentation root and `package` for a workspace
package. A stage callback receives progress names and integer counts. No model
call occurs inside `run_scan`; optional CLI model assessment is a separate layer.
""",
    "how-it-works.md": """# How it works

```mermaid
flowchart LR
  A[Documentation text] --> B[Located claims]
  C[Source text and AST] --> D[Static symbol index]
  B --> E[Scope and exact matching]
  D --> E
  E --> F[Bounded Git patch history]
  F --> G[Evidence and reports]
```

Rules decide literal claims first. Optional model assessment handles selected
anchored prose and cannot override code-decided findings; it must cite supplied
source text that passes validation.

History reads one bounded newest-first patch stream, at most 2,000 commits and
8 MB/12 seconds per stream. Truncation is explicit. Warm scans reuse bounded,
snapshot-keyed Git text and source-index caches. Clear the local index cache
when investigating changed scanner behavior; public jobs trust neither cache
from a target repository.
""",
    "graph.md": """# Graph schema and queries

```mermaid
flowchart LR
  S[Section] --> C[Claim]
  C --> Y[Symbol]
  Y --> F[Source file]
  F --> K[Introducing commit]
  Y -. absent source .-> K
```

The HTML report uses a bounded Cytoscape graph. Node IDs are stable claim/section/
symbol IDs or commit SHAs; edges retain the corresponding claim and verdict.
The web graph is capped at 200 nodes; the graph API export at 1,000, with a
`truncated` flag. Offline HTML and JSON exports are available without a database.
No FalkorDB-backed query server is implemented; the unused dependency was removed.

In the HTML report browser console, the actual Cytoscape instance is `cy`:

```javascript
cy.nodes('[kind = "claim"]')
cy.edges('[verdict = "renamed"]')
cy.nodes().filter(node => node.data('kind') === 'commit')
```

Click a finding to follow its evidence path. Filters, insights, keyboard selection,
and the graph are views of the same scan, not independent detectors.
""",
    "llm-mode.md": """# Optional model mode

Rule-only operation makes no model requests. CLI `--llm` explicitly enables
selected prose verification through OpenRouter and the configured model.
Set `OPENROUTER_API_KEY` in the environment; do not put keys in source files.
The scanner never loads dotenv files.

`DOCS_DRIFT_LLM_MODE=replay` uses recordings and sends nothing; missing recordings
fail with a useful message. Live/record mode can send selected documentation,
redacted source excerpts and relative locations to OpenRouter/model providers.
Responses must cite supplied source and pass schema/citation validation. This
reduces prompt-injection risk; it is not proof of model correctness.

Use a spending cap and review prompts, provider privacy terms, logs and recordings
before enabling live mode on private code. See [security](SECURITY.md) for exact
data boundaries and known limits. Public web scans and the composite Action are
rule-only and have no model-enable input.
""",
    "LIMITATIONS.md": """# Limitations

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
""",
    "faq.md": """# FAQ

## Why not just grep?

Grep finds strings. docs-drift locates documented claims, checks scoped static
declarations/defaults and attaches Git evidence where available. It remains a
partial detector, not a proof that documentation is correct.

## Does it run my code?

No. It parses target text/AST and invokes Git with argument lists. It does not
import target modules, install target dependencies, or run fenced commands.
The self-documentation command tests execute only this project's trusted CLI.

## Will it nag me about my changelog?

No. Default discovery excludes changelogs and known historical sections. Explicit
inline suppressions are available for documented examples outside this project's
scope, but should have a reason and never hide real product documentation drift.

## What about private repos?

Use the CLI on a local checkout. The public web demo accepts only public GitHub
repositories. Rule-only mode sends no model requests; optional model mode sends
selected redacted snippets externally as documented in [security](SECURITY.md).

## Is a missing result definitely a defect?

No. An index miss, unsupported framework, sample path or third-party tool can
explain it. Human review and the pending public benchmark are essential.
""",
    "built-with-voice.md": """# How I built it

**AUTHOR TO WRITE — PERSONALISE THIS PAGE.**

## Workflow

Your dictated account of using Wispr Flow for the No Hands challenge.

## What worked / what did not

Your experience; no invented anecdotes.

## Usage card

Your real usage-card figures, not estimated numbers.

## Real dictated prompts

One or two prompts you choose to share, with secrets removed.
""",
    "benchmark.md": """# Real-repository benchmark

Human accuracy labels are pending. Do not substitute fixture numbers or AI
opinions for real-world precision/recall. The preregistered protocol, exact repo
SHAs, source snapshot, runtime measurements and blank human labeling sheet are
under `benchmark/` in the source checkout.

After labeling, `python -m benchmark.compute --labeler "Your name" --final`
produces per-repository/per-verdict precision with Wilson 95% intervals and a
qualified weighted recall audit. False-positive categories come from the human
notes. Old dated runs are retained; changed detector rules require a new run and
sample. Upstream drafts are prepared only for human-verified true labels.
""",
}


def main() -> None:
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    for name, text in PAGES.items():
        (docs / name).write_text(text, encoding="utf-8")
    (docs / "configuration.md").write_text(render_reference(), encoding="utf-8")
    (docs / "CHANGELOG.md").write_text(Path("CHANGELOG.md").read_text(encoding="utf-8"), encoding="utf-8")
    policy = Path("SECURITY.md").read_text(encoding="utf-8").replace("docs/SECURITY.md", "SECURITY.md")
    (docs / "security-policy.md").write_text(policy, encoding="utf-8")
    (docs / "support.md").write_text(
        "# Supported ecosystems\n\n" + support_table() + "\n\nNo framework is claimed fully supported.\n"
    )
    (docs / "license.md").write_text("# License\n\n```text\n" + Path("LICENSE").read_text() + "\n```\n")
    (docs / "contributing.md").write_text(Path("CONTRIBUTING.md").read_text(encoding="utf-8"), encoding="utf-8")
    sync_index(docs)


def sync_index(docs: Path) -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    for old, new in {
        "docs/": "",
        ".github/workflows/ci.yml": "github-action.md",
        "pyproject.toml": "getting-started.md",
        "](LICENSE)": "](license.md)",
        "CONTRIBUTING.md": "contributing.md",
        "SUBMISSION.md": "submission.md",
        "RELEASING.md": "releasing.md",
        "benchmark/PROTOCOL.md": "benchmark.md",
        "benchmark/RESULTS.md": "benchmark.md",
    }.items():
        readme = readme.replace(old, new)
    (docs / "index.md").write_text(readme, encoding="utf-8")
    for source, target in (("RELEASING.md", "releasing.md"), ("SUBMISSION.md", "submission.md")):
        if Path(source).exists():
            (docs / target).write_text(
                Path(source).read_text(encoding="utf-8").replace("(docs/", "("), encoding="utf-8"
            )


if __name__ == "__main__":
    main()
