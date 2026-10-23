# Product direction and release gates

Primary adoption model: **self-hosted + GitHub Actions**, selected by the owner
on 22 October 2026. The product should give maintainers a repeatable way to catch
new documentation drift, inspect evidence and accept or fix it through review.
Hackathon presentation is one distribution opportunity, not the product boundary.

## Available in the development checkout

| Capability | Current boundary |
| --- | --- |
| Static scanner, evidence, history | Supported ecosystems only; runtime semantics are not proved |
| Monorepo/package scoping | Existing CLI support; validate against each pilot's layout |
| Incremental adoption | Reviewed baselines, confidence thresholds and budgets; changed verdicts reopen review |
| GitHub Action | Linux, read-only token, full + new reports and explicit gate JSON/outputs |
| Organization portfolio | Explicit local manifest, sequential scans, static dashboard and error isolation |
| Reports | JSON, SARIF, HTML and other existing CLI formats; no hosted account required |
| Git failure handling | Checkout scans stop if tracked source listing fails; they do not silently traverse generated trees |
| Public judge scanner | Bounded anonymous demo; single-process queue, not a multi-tenant organization backend |

## Required before a supported production release

**Reliability:** Run the complete supported-platform matrix after publishing a
reviewed commit. Exercise package installation, container execution, interrupted
scans and report recovery. Publish reproducible large-repository measurements;
set supported size/runtime limits from observed results. Existing fixture tests
and a small real-case review do not establish organization-scale reliability.

**Finding quality:** Independently label a declared sample across multiple
supported ecosystems, publish false positives, false negatives where measurable,
and abstentions. Choose CI defaults from maintainer feedback. Do not label an
unverified claim a confirmed defect or advertise an unmeasured accuracy number.

**Supply chain:** Publish a versioned release, verify PyPI installation and
trusted publishing, generate/verify provenance and dependency inventory, document
upgrade/rollback and disclose supported versions. Organizations must pin the
Action's reviewed commit and review dependency changes. PyPI remains unpublished.

**Operations:** Prove deployment on the chosen host, define retention, backups,
runner resource limits, logs and alert ownership. Private artifacts require the
organization's access controls. The static portfolio currently retains all runs;
retention is operator-managed. Do not promise an SLA before measuring operation.

**Maintainer workflow:** Trial the report-only → reviewed baseline → new-drift
gate path in real repositories. Track review burden and accepted corrections.
Document ownership/triage and false-positive reporting. No upstream adoption or
acceptance is implied by inspecting a public repository.

The [maintainer pilot](MAINTAINER-PILOT.md) now prepares a frozen local review
pack and records self-reported feedback without prefilled labels or an inferred
adoption claim. Independent maintainer participation and retained CI runs are
still required to validate product demand.

## Subsequent product work

The [initial Flask/Express trial](ORG-PILOT-2026-10-22.md) found three false
positives in a selected three-finding inspection. Treat detector quality as a
release blocker for default enforcement on those projects; the functioning
portfolio and Action are not evidence that the detector is ready for wide use.

- CODEOWNERS-aware routing and review decisions with reasons and expiry.
- Changed-code prioritization that preserves full-scan coverage and evidence.
- Multi-run trends and scan comparisons with versioned identities.
- Optional GitHub App installations with least-privilege repository selection,
  authenticated administration and auditable webhook processing.
- If a hosted organization service is later requested: tenant isolation, durable
  jobs, retry/idempotency, private-repository credentials, authorization and
  operational monitoring before exposing organization data.

These are planned capabilities, not implemented claims. The initial organization
workflow is documented in [self-hosted adoption](ORGANIZATIONS.md).
Local checks and their limits are recorded in [verification](ORG-VERIFICATION-2026-10-22.md).
