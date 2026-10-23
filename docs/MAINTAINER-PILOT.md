# Test whether maintainers want this in CI

The product hypothesis is repeatable checks of exact documentation contracts on
every PR: flags, environment names, literal routes, declared symbols and supported
command examples. A static match does not prove runtime behavior. The current
selected real-case reviews do not establish product accuracy or willingness to adopt.

Start with 2–3 willing maintainers of supported SDK, CLI or API repositories.
Ask permission to scan and share their findings. No invitation has been sent and
no maintainer participation is claimed by this document.

The owner's two supplied candidates now have [pinned local review packs](MAINTAINER-PILOT-2026-10-23.md).
Their labels and adoption feedback are still pending.

## Freeze one review per repository

Use a clean checkout at a recorded revision. From this scanner's source environment:

```sh
uv run python -m scripts.maintainer_pilot prepare --repo /path/to/clean-checkout --repo-url https://github.com/OWNER/REPO --out /path/to/new-pilot-run
uv run python -m scripts.maintainer_pilot report /path/to/new-pilot-run
```

The output directory must be new and outside the target checkout. The command
does not install dependencies, execute examples, use a model or alter CI. It
preserves the scan, HTML report, source snapshot, actual configuration, scan time,
warnings and a deterministic sample of at most 25 qualifying findings. Use a new
directory and seed for later detector versions. Existing frozen benchmark runs
and human labels are never changed by this workflow.

Repository selection is purposeful, not representative. The existing benchmark
protocol supplies the labeling criterion: would a reader following the statement
at this revision be wrong or fail? Inspect source and surrounding context before
labeling. An index miss, third-party instruction or intentional example is a false
positive, not a defect in the target repository. A zero-finding run is not proof
of coverage; unverifiable and low-confidence counts remain in the scan.

## Review together, then ask about the next PR

The maintainer fills the blank human-verdict and note columns in the generated
label sheet. Every reviewed row needs a source citation or explanation. Leave
uncertain rows blank; no AI labels should be entered as human labels.

In the generated feedback file, record the actual reviewer's name/role, explicit
human-review attestation, review minutes, whether they would keep it in CI, their
decision (report-only, new-drift-gate or disabled), and their reason. Correction
URLs are self-reported and require separate verification before claiming adoption
or upstream acceptance. The report labels all feedback as self-reported.

Re-run the report command after review. Missing/duplicate/deleted review rows,
changed frozen inputs, unattributed labels and notes missing from reviewed rows
are rejected. Partial reviews retain counts but no precision number. Complete
reviews show selected-sample precision and a Wilson interval, with population and
sample sizes. Recall is always unmeasured in this pilot; use the separate frozen
benchmark audit to investigate missed claims.

Ask: Which finding would you act on? Which false positive would make you disable
this? How long did review take? Would you keep report-only scans on normal PRs?
Record a refusal or disabled decision as a result. Compare against the team's
existing review process; don't presume a standalone tool is needed.

## Try report-only on real PRs

Reuse the existing composite Action and artifact upload example in
[the Action guide](github-action.md). Pin a reviewed scanner commit and set
`fail-on-drift: 'false'`. Retain its full scan and gate artifacts, including errors
and abstentions. This pilot tool does not enable a blocking gate automatically.

For several normal PRs, record PR URL, target SHA, report artifact, scan runtime,
review minutes, useful findings, false positives, incomplete scans and the actual
maintainer decision. If blocking is later requested, review the baseline and
confidence policy together; a baseline accepts existing debt and does not repair
detector errors. Keep errors visible and preserve artifacts on failure.

Proceed to broader adoption only after maintainers choose to retain it, useful
findings recur, and review burden is acceptable to them. Human interviews,
independent labels, real PR artifacts and adoption evidence remain pending until
they actually exist. No universal accuracy threshold or SLA is inferred here.
