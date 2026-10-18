# Judge's assessment: 18 October 2026

**Promising evidence-led developer tool, with release readiness still unproven.**
The strongest story is a concrete failed command followed by a precise stale
token, source evidence, the introducing commit and a reviewable patch. That is
easy to understand quickly and useful to maintainers. The CLI, report formats,
graph, CI integration and editor path give several ways to use the same analysis.
Static inspection avoids running arbitrary target repositories. Offline reports,
privacy controls, bounded processing and explicit unsupported cases demonstrate
care beyond a polished landing page.

The local engineering evidence is substantial: release validation passed 560
Windows tests against an installed wheel; overall coverage reached 6272/7373,
with the extraction/matching gates also passing. Pinned scans/human-review
sheets are reproducible. The random robustness sweep completed 29 repositories
with one timeout and no observed scan crashes. Keeping failures and blank labels
visible is more credible than displaying fixture scores as public accuracy.

The largest weakness is proof of actual user value. There is no supplied personal
incident yet, no human accuracy labels, no maintainer-merged upstream fix, and no
public release or deployed demo verified in this release session. Reviewers could
see a broad implementation but still wonder whether it reliably distinguishes
stale instructions from intentional examples. Partial framework coverage, TSX
recovery warnings, sparse-checkout omissions and ambiguous matching increase
that concern. The observed speed targets fail, and one Windows timeout needed
manual cleanup. Do not describe the product as universally deterministic or
crash-proof based on these limited checks.

The presentation also depends on unfinished evidence: the owner's voice-building
story/weekly Insights cards, a real PR comment and disappearing editor squiggle,
an honest <2-minute video, and a sign-in-free public link. A localhost screenshot
or generated fixture cannot replace those challenge requirements. Release
automation and docs help, but judges need to open the project and see it work.

## Three highest-impact improvements, ranked

1. **Prove one real problem end to end.** Supply your own incident; human-check a
   small real finding; show scan → evidence → reviewed diff and, if possible,
   an accepted upstream fix. Complete the preregistered human labels before
   reporting precision/recall. Do not cherry-pick one success as overall accuracy.
2. **Make the first visit and install dependable.** Finish the reviewed TestPyPI
   and stable releases, enable approved hosting, run anonymous phone/laptop and
   live-repo smoke checks, and keep monitoring on through judging. A reliable
   cached example is useful when labeled as cached; keep live failures visible.
3. **Narrow the promise and remove recording friction.** Focus the 1:53 video on
   one supported, verified flow. Improve measured package lookup/file-read costs
   and timeout cleanup on a new benchmark run, then update limitations and results.
   Preserve the old measurements; do not make an unmeasured speedup claim.

With twelve days until the supplied deadline, prioritize verified outcomes over
adding another ecosystem or model feature. Freeze production through judging and
keep later experiments on a branch.
