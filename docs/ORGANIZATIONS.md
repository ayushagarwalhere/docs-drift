# Self-hosted adoption for open-source organizations

docs-drift can run on an organization's own checkouts and CI runners. Source
does not need to be uploaded to the public demo, and the rule-based workflow
needs no model key. This is an unreleased development product; do not interpret
these workflows as an audited enterprise release or a hosted service SLA.

## Start with one repository

Install from a reviewed source checkout with the locked environment:

```sh
uv sync --locked --all-extras
uv run docs-drift scan --repo /path/to/project --format html --output scan.html
uv run docs-drift baseline update --repo /path/to/project
uv run docs-drift check --repo /path/to/project --baseline .docs-drift-baseline.json
```

Review the first report before accepting a baseline. A baseline is accepted
existing debt, not a list of confirmed defects. Commit changes to the baseline
through normal review. Require maintainer review of scanner configuration,
workflow and baseline changes so a pull request cannot silently relax its gate.
The same claim with a changed verdict is treated as new drift.

Start CI in report-only mode; after reviewing false positives, enable a gate on
new drift. Keep low-confidence and unverifiable findings visible in full reports.
Static matching does not prove runtime behavior. Check the language/framework
[support matrix](support.md) before deciding what the gate means for your repo.

## Scan multiple checkouts

Create an organization workspace with local checkouts:

```text
organization/
  organization.toml
  checkouts/
    api/
    cli/
  reports/
```

`organization.toml`:

```toml
schema_version = 1

[[repositories]]
name = "api"
path = "checkouts/api"
baseline = ".docs-drift-baseline.json"
min_confidence = 0.8
max_findings = 0

[[repositories]]
name = "cli"
path = "checkouts/cli"
# Without a baseline, all drift is new. Confidence defaults to this repo's config.
max_findings = 0
```

From the scanner source environment:

```sh
uv run docs-drift portfolio --repo /path/to/organization --manifest organization.toml --report-dir reports --report-only
```

Open `reports/index.html`. It links to the run's immutable JSON summary and each
checkout's complete HTML report. Each repository also has `scan.json`,
`scan.sarif` and `gate.json`. Remove `--report-only` to enforce the policy.
Exit codes: **0** means completed within the chosen policy, **1** means at least
one drift gate failed, **2** means invalid input or at least one scan error.
Scan errors remain errors even in report-only mode; the other scans continue.

The manifest supports 1–100 explicitly named local checkouts. This is an input
bound, not a measured capacity guarantee. Scans are sequential to bound concurrent
resource use. Repository paths must remain inside the selected workspace;
baselines must remain inside their repository. The report directory must be
outside all scanned checkouts. Inputs are data; no cloning, target installation,
target code execution or model invocation happens in this command. Per-repository
`.docs-drift.toml` / `pyproject.toml` settings govern the scan.
Entries require a checkout root with a `.git` marker by default, including Git
worktrees. To intentionally scan an unversioned source snapshot, set
`require_git = false` on that entry. Its lack of revision/history is surfaced in
gate warnings. UTF-8 manifests with a Windows byte-order mark are accepted.

Every run gets a new directory. A failed scan never links to an old successful
report. Latest HTML/JSON files are replaced individually; dashboard report and
JSON links always refer to its own run. Older runs are retained deliberately:
schedule one writer per output directory and apply your own retention policy.
No automatic repository discovery, refresh, distributed queue or cross-run trend
aggregation is claimed.

## GitHub Actions

The composite [Action](github-action.md) now accepts `baseline`,
`min-confidence`, `max-findings` and `fail-on-drift`. It produces:

| Artifact | Meaning |
| --- | --- |
| `scan.html/json/sarif/md` | Full findings, including accepted debt and unverifiable claims |
| `new.json/sarif/md` | New drift subset, including findings below the gate confidence |
| `gate.json` | Versioned gate policy, SHA, counts, status and redacted warnings |

Outputs include `report-directory`, `gate-status`, `new-drift`,
`qualifying-new-drift` and `baseline-drift`. Reports and outputs are written
before a drift gate exits 1, allowing a later `if: always()` artifact-upload step.
Each Action invocation gets its own report directory, so multiple repository
steps in one job do not overwrite each other's evidence. Use the returned output.
Invalid policy or a missing/malformed selected baseline fails before scanning;
it does not silently disable the gate. Baselines are bounded regular metadata
files, at most 1 MB; a symlink baseline is rejected by the Action.

The Action currently runs on Linux. Pin its trusted source to a reviewed full
commit once this change is published. Keep target checkouts separate from the
trusted Action source. Use read-only permissions, avoid executing fork code in
privileged `pull_request_target` workflows, and keep installation credentials
out of scan environments. These practices follow
[GitHub's secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use).

## Operate an organizational pilot

1. Choose a maintainer and two repositories with supported frameworks.
2. Review a declared sample including false positives and unverifiable findings;
   record exact revisions and labels. Do not infer accuracy from synthetic tests.
3. Run report-only on normal pull requests and scheduled full scans. Measure
   runtime, peak memory, incomplete history and noisy findings on those repos.
4. Review the baseline and policy, then enable a new-drift gate. Preserve artifacts
   on failures and keep a manual override/review process.
5. Pin the scanner revision and dependencies, set artifact retention and access
   controls, and test rollback before wider rollout.

Use clean checkouts for reproducible reports. The reported Git SHA identifies
HEAD; it does not by itself identify uncommitted working-tree changes.

Use an isolated CI runner/container with CPU, memory, disk and wall-clock limits.
The local portfolio command has no organization-wide timeout; the public demo's
two-minute job limit is not a general large-repository limit. Avoid exposing the
public demo API to private repositories: it has no tenant authorization system.

Reports may contain source and documentation snippets. Serve them only through
your existing access controls, retain them according to your policy and keep
baseline/configuration edits auditable. Secret-pattern redaction is best effort,
not a guarantee that every confidential value will be detected.

See [product maturity and release gates](PRODUCT.md) for what remains before a
broad production release.

The [first multi-repository trial](ORG-PILOT-2026-10-22.md) includes observed
false positives and the wrong-root input mistake; it is not a success-only case
study or an accuracy benchmark.
