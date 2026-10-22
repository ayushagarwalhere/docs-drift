# Judge experience verification — 22 October 2026

Local Windows/Python 3.13 verification, not a public uptime or performance claim.
The owner deferred deployment to a later AWS task. Nothing was published to PyPI,
GitHub Releases, or the event. No OpenRouter key was used or required.

## Implemented and exercised

- Landing page has a public repository URL input and scan button, with no login.
- A real API submission scanned `https://github.com/textualize/rich-cli` through
  the disposable production worker: **done**, **cached=false**, **14.137 seconds**
  total, **6.820 seconds** clone time. Local scan ID:
  `ea5ffdeaad834cfe8ffb52eb3c6f1081`. This live scan is separate from the pinned
  review case and must not inherit its review labels.
- Smoke script checked origin handling, security headers, a baked report, a real
  scan, HTML output, and schema-v1 JSON. It passed on `http://127.0.0.1:8091`.
- HTTP 200: landing, case study, Flow page, reviewed report, graph endpoint, both
  raw scans, runtime observations, review provenance and both original cards.
- Pinned rich-cli review: two option errors reproduced (exit 2); two corrected
  commands succeeded (exit 0). All four saved drift findings reviewed, including
  two false positives. Fresh static scan flags only the two bad options.
- `docs-drift index --find=line-number` ranks the correct plural declaration
  first; manual Git blame/show identifies doc commit `567f6c84`. The scan itself
  supplies no introducing commit for this typo; no automatic attribution claimed.
- Fixed default browser origin from port 8000 to the server's default port 8080.
- Replaced unavailable PyPI install advertising with a source-checkout command.
- Original Flow images copied from the owner's supplied paths; SHA256 provenance
  included. No account totals presented as project-only usage.
- Rebuilt the UI with monochrome themes, scroll-driven perspective, reduced
  motion support, a prominent scanner, instant real samples and searchable
  reports. [UI changes and reproducible DOM checks](UI-REVIEW.md) document the
  verification and remaining visual checks. Web/report/security tests: 42 passed.

## Verification limits and remaining work

- No connected browser was available for visual/UI automation. HTTP/service tests
  passed; visual phone/laptop inspection remains for the owner.
- Docker CLI was present but the Docker daemon was unavailable. Wheel/sdist
  content validation passed; production container execution still needs a
  Docker-capable host.
- The four-finding review is agent-assisted, selected, and small. Independent
  human benchmark labels, overall accuracy and recall remain pending.
- The Flow cards do not prove which build prompts were dictated. Actual dictated
  requirements/revisions and an original Flow-use clip remain needed.
- Final video (<120 seconds), captions, video/project/demo links, event card
  uploads and physical-device checks remain incomplete.
- Public AWS deployment and PyPI trusted-publisher setup/publication remain
  separate launch tasks. Do not claim the unrelated Vercel address as this demo.

See [AWS handoff](AWS-HANDOFF.md), [real case](REAL-CASE.md),
[Flow evidence](built-with-voice.md), and [recording plan](FLOW-RECORDING.md).
