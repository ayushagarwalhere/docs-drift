# GitHub Action

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

For gradual adoption, pass a reviewed `baseline` path relative to `repo`.
`min-confidence` optionally overrides the repository threshold; `max-findings`
defaults to zero allowed qualifying new findings. `fail-on-drift: 'false'` is
report-only mode. Invalid input or a missing selected baseline fails before the
scan; a changed verdict for a baselined claim requires review again.

Full findings remain in generated `scan.*` reports.
The generated artifacts `new.json`, `new.sarif` and `new.md` contain the new-drift subset.
The generated `gate.json` artifact records the policy, revision, counts, status and redacted warnings.
Scalar outputs are `gate-status`,
`new-drift`, `qualifying-new-drift` and `baseline-drift`. Artifacts and outputs are
written before a failing drift gate exits, so artifact upload should use
`if: always()`. A passing gate does not prove documentation correctness.
See [organization adoption](ORGANIZATIONS.md) for multi-repository scanning,
review and operating guidance.

The Action never posts PR comments, accepts model/API keys, or calls a model.
It uses no write token. Do not use `pull_request_target` to check out PR code with
secrets/write permissions. Fork PR checks need only read permissions. JSON/HTML
and Markdown are redacted at serialization; repository caches are not trusted.

![Generated PR report preview, not a submitted GitHub comment](assets/pr-comment-preview.png)

This image is a preview of the generated Markdown, not proof of a posted comment.
