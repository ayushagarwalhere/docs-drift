# Reproduce and review the benchmark

Read [the protocol](PROTOCOL.md) before reviewing
[the blank human sheet](labels_todo.md). The current frozen run is
`2026-10-18-r4`; its source hash, exact repository pins, sampling seeds,
source archive and measurements are retained in that run directory.
The recorded Git commit alone is insufficient: this run used a dirty checkout.
`run.py` also writes `scanner-source.zip` (the measured product source) into each run; those archives stay local.
Release packaging now adds version metadata and changes public report links, so
the current source digest differs from r4. Compute the frozen run in an isolated
checkout with r4's archived src, pyproject and lockfile restored, then copy your
completed label sheet there. Do not overwrite the primary working checkout or
relax the freeze guard. This keeps the recorded version/hash and original rules
attached to the human-reviewed sample.

Large raw scans and diagnostic profiles stay local. The committed consolidated
measurements and samples are sufficient to compute the report after human
labeling, without regenerating those raw scans. Keep each row ID unchanged and
fill the human verdict and note cells. Precision uses true/false; recall uses
stale/current/not-a-claim. Read the linked documentation and code at the pinned
commit. Do not infer a true label from the tool's confidence.

From the project root, inspect a pending report with:

```sh
python -m benchmark.compute --run 2026-10-18-r4
```

After completing all rows, replace the reviewer name below with your own name:

```sh
python -m benchmark.compute --run 2026-10-18-r4 --labeler "Your human name" --final --drafts
```

Final publication requires every human label and locks the label-file hash.
Drafts are local review files, never submitted issues or PRs. They are generated
only for human-confirmed drifts. Preserve the old run when changing detector
rules; create a new dated run and sample instead.

The separate robustness sweep has no correctness assertions and disables
history and model calls. Its report distinguishes completed scans, timeouts,
acquisition failures and crashes. It must not be quoted as an accuracy score.
