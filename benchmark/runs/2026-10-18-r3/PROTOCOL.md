# Real-repository benchmark protocol

Preregistered: **2026-10-18**, before any results from this benchmark were viewed.
Protocol version: **1**. Selection seed: **20261018**. Finding sample seed:
**20261019**. Recall sample seed: **20261020**. Robustness seed: **20261021**.

## Scope and freeze

The 12 repositories are selected for ecosystem coverage, not expected accuracy:
pre-commit/pre-commit (argparse), simonw/sqlite-utils (Click),
fastapi/fastapi-cli (Typer), fastapi/full-stack-fastapi-template (FastAPI),
pallets/flask (Flask), django-oscar/django-oscar (Django), tj/commander.js
(Commander), yargs/yargs (yargs), expressjs/express (Express), dubinc/dub
(Next.js/monorepo), encode/httpx (documentation site), and withastro/astro
(TypeScript/monorepo). Framework repositories include framework examples and are
not claimed to represent only deployed services. Size/activity are recorded,
not presumed. Unavailable repositories stay recorded as failed attempts; any
replacement requires a new explicitly recorded selection/run.

Resolve each default-branch HEAD once and save its exact SHA in `repos.json`.
Record scanner version, Git commit, dirty-worktree status and a SHA-256 digest of
the actual scanner sources/configuration. A dirty source digest identifies this
uncommitted implementation honestly; HEAD alone cannot reproduce uncommitted
changes. Archive each run's protocol, source snapshot, configuration, commands,
raw result JSON, runtime, clone information and sampling population metadata.

Complete hardening before freezing the measurement run. Never change detection
rules within a run, between labeling and computing its metrics, or silently
rescan a labeled sample. A later fix creates a separate dated run using new
repos or a different seed/sample; preserve previous results and labels. Frozen
runs refuse a source/protocol/config mismatch.

## Detector configuration

Use the default confidence threshold (**0.6**), rule-only mode, default discovery
including detected documentation sites, source indexing, and Git history with a
maximum of **2,000 commits**. No model calls, dependency installation or execution
of target repository code. Acquire depth <= 2,000 with blob:limit=100000 and sparse doc/source
checkout; record shallow/truncated history and any acquisition limits explicitly. Infrastructure failures,
parser warnings and unsupported ecosystems remain visible and never count as
correct predictions. Runtime excludes network clone unless labeled end-to-end.

## Precision sample and human labels

1. Run docs-drift at each pinned SHA.
2. Keep every drift finding (`missing`, `renamed`, `default_changed`,
   `invalid_command`) whose confidence is at least 0.6. Sort by stable finding
   identity. Keep all if at most 25; otherwise sample 25 uniformly without
   replacement using seed 20261019 plus a SHA-256-derived repository identifier
   (never Python's randomized hash). Save the full population size and selection.
3. The **project owner, a human**, reads the exact doc line and code at that SHA.
   Criterion: **a reader following this doc statement at this commit would be
   wrong or fail**. Historical references, intentional illustrative examples,
   third-party tool commands, unsupported inference and index omissions are
   false positives. Read context; absence from an index is not proof of absence.
4. Enter `true` or `false` in the human verdict column. Supply a note, especially
   a short pattern category for false positives (`third-party`, `historical`,
   `example`, `index-miss`, `dynamic`, `other`). Never prefill these labels with AI
   judgments. No AI second-opinion column unless the owner requests it.

## Recall audit (separate sample)

Sample **30 candidate doc statements per repository** without replacement using
seed 20261020 plus the stable repository identifier. Construct two independent
strata: lines with extracted `ok` claims, and nonblank substantive doc lines
(at least 20 characters, excluding headings/separators) that have no extracted
claim location. The second stratum is built directly from raw documentation,
not from the extractor's candidate recognizer. Prefer 15 from each stratum;
reallocate shortages, record population/selected sizes, and retain all if fewer
than 30 exist. The unit is the entire located statement/line in context, not
an invented detector claim. No statement containing a detector drift finding
is eligible for this audit. Record ambiguity and non-claims explicitly.

Human audit verdicts: `stale`, `current`, or `not-a-claim`; notes explain the
classification and cite code. Empty cells remain unlabelled. Sampling doc lines
is an imperfect proxy for sampling all semantic claims; report that limitation.

Estimate missed stale statements by stratum: population size × observed stale
fraction among all audited candidates (not-a-claim contributes zero stale). Estimate true positive population
from precision labels with the corresponding repository sampling weight. Then
estimated recall = estimated true positives / (estimated true positives +
estimated missed stale statements). This is a **weighted audit estimate**, not
exhaustive recall. `unverifiable` and low-confidence predicted findings are
reported separately and are not in this two-stratum audit; consequently the
estimate does not establish recall over those populations. Do not present a
binomial Wilson interval as a valid interval for this weighted recall ratio.

## Reporting

Precision tables: per repo and per verdict, successes/labels, sample/population
sizes, Wilson 95% binomial intervals (z=1.959963984540054). Aggregate metrics
show sampling weights; unweighted sample precision is clearly labeled. Pooling
per-verdict labels across repositories is descriptive and can be sample-biased.
Missing labels are never assumed true, false, current or stale. Incomplete runs
show pending metrics; final reporting requires all selected human labels and the
human labeler's name. No accuracy badge or unqualified README number before that.

Coverage: unverifiable / all findings, plus extracted claim counts, parser/file
warnings and unsupported ecosystems. Record runtime per repo and acquisition
failures. List false-positive patterns with human notes, counts and exact links.
All dates, source identities, commands and confidence intervals remain visible.

Upstream drafts and fix patches are prepared **only after human-verified true
labels**. Never claim the owner checked a finding until that label exists. Drafts
are not submitted, emailed or posted automatically.

## Performance and robustness

Performance timings use independent runs so concurrent clone/scan work does not
contaminate comparisons. Cold clears scanner caches; warm repeats at the same
commit with history enabled. Report every raw timing, memory method, scope and
failed target. Fixture target <1s each warm; Flask <20s cold/<3s warm; a pinned
repository with >5,000 tracked files <90s cold; peak memory <1GB; warm impact <5s.
Do not round failures into passes. If large scan exceeds 90s, archive cProfile
hot spots, fix the top two useful bottlenecks, then preserve before/after runs.

Robustness frame: first 100 star-sorted, non-fork, non-archived GitHub search API
results with >1,000 stars per language. Randomly select Python 8, JavaScript 8,
TypeScript 7, Go 7 with seed 20261021. This is random within a disclosed frame,
not uniform over all GitHub. Save API selection/pins before scanning. Distinguish
clone/limit failures from scanner crashes; never count an unscanned repo as a
successful crash-free scan. Group warning messages and retain examples.

Hardening amendment before the frozen labeled run: attribution uses one bounded 8 MB/12-second patch stream and reports truncation; robustness scans exercise extraction/index/matching without history or models, with depth-1 acquisition allowed. These are not attribution tests. Criterion, confidence and sampling seeds are unchanged.

Before any human labeling: own-repo dogfooding found that an ambiguous default
could be incorrectly promoted to a rename despite its subject still existing.
Run r1 is retained as a superseded hardening attempt, without accuracy labels.
The correction is frozen as r2; its finding seed is **20261022** and recall seed
is **20261023**. The selection, criterion and default confidence remain fixed.

Pre-label Windows regression amendment: committed-document blame now explicitly
reads HEAD, so global CRLF conversion settings cannot turn a committed line into
a synthetic, time-varying "not committed" attribution. Run r2 is retained as
another superseded hardening attempt, without human labels. The final r3 source
uses finding seed **20261024** and recall seed **20261025**. Retry time limits
may be increased for slow repositories; previous timeout attempts remain visible.
