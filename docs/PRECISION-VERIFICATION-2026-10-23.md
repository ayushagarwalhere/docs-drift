# Local precision and CI verification — 23 October 2026

Verified on Windows / Python 3.13.7. These are local engineering checks, not
GitHub runner results, a published release or independent accuracy evidence.

## Changes

- Environment-assignment values, custom URI schemes, scoped packages, import
  aliases, protocol-like slash names and function-call arguments no longer become
  unsupported high-confidence missing-file assertions. Ambiguous references stay
  visible as unverifiable; explicit missing-file controls still fail.
- Generated outputs, explicit file-creation instructions and external references
  retain their context. Source references with line suffixes resolve the filename.
- Complete shell-command validation does not invent subcommands from paths,
  numbers or option values. End-of-options handling also applies to extracted
  flag claims. Command examples remain static checks, not runtime verification.
- Valid pnpm root members are supported after a supplied-repository scan exposed
  a crash. The original failed attempt and earlier outputs remain documented.
- The narrowly scoped secret-scan exception now includes the generated artifacts
  that triggered the failure. A CI step recomputes hashes in current files and
  fetched history. An unrelated secret-shaped field in the same artifact path
  still fails a local positive control.
- The real rich-cli case now has an applicable, explicitly reviewed patch and
  before/after source-matching proof. Patch bytes are preserved through Git and
  whitespace hooks; the release resource check verifies the packaged checksum.
- The maintainer pilot reuses the existing benchmark label contract, freezes its
  inputs, preserves blank labels, rejects changed/deleted inputs and records
  self-reported CI willingness without enabling enforcement.

## Final checks

| Check | Result |
| --- | --- |
| Disjoint final regression groups | 668 tests passed; zero test failures/errors/skips |
| Combined package/scripts coverage | 6917 / 7949 = 87.02%; unchanged 85% gate passes |
| Extraction coverage | 1218 / 1306 = 93.26%; unchanged 90% gate passes |
| Matching coverage | 333 / 352 = 94.60%; unchanged 90% gate passes |
| Ruff lint and formatting | Pass |
| Strict mypy, Linux and Windows platform analysis | Pass; 159 files; not native Linux execution |
| Strict MkDocs build | Pass |
| Actual CLI own-repository check | Exit 0; no threshold reduction or blanket baseline |
| Gitleaks history scan | 12 commits; no findings with the scoped, verified exception |
| Generated grouping hashes | 928 occurrences recomputed across 20 current/historical documents |
| Secret positive control | Rejected, including an unrelated key field at an allowlisted artifact path |
| Reviewed rich-cli correction | Applicable patch; both corrected claims remain extracted and match declarations as `ok` |
| Local wheel / sdist | Built as explicitly local development version 0.1.0.dev0; resource contract and patch checksum pass |

The final test set ran in four disjoint groups. Each group inherited the global
85% coverage check and therefore returned a coverage-only failure for its subset,
despite every test case passing. Coverage was then combined and all unchanged
global/directory gates were enforced successfully. No threshold was lowered.
The final aggregate summary, XMLs and coverage JSON are preserved locally under
.docs-drift/ with the precision-shard and precision-final prefixes.

An earlier full run recorded 667 passing tests and one patch-checksum failure:
whitespace cleanup had removed required unified-diff context markers. That
failure was fixed rather than hidden, the three artifact/web/package checks were
rerun successfully, and the final disjoint groups above include the repaired
checksum test. Patch byte-preservation settings must be included in any commit.

## What remains unverified

[Nova and Prism.ai review packs](MAINTAINER-PILOT-2026-10-23.md) contain 30 blank
human-review rows. Maintainer feedback, representative precision/recall, false
negative rates and retained CI adoption are still unknown. These repositories
informed development and are not a held-out evaluation. Unsupported patterns and
the 264 Prism.ai parser/coverage warnings remain visible.

The two reproduced rich-cli defects have scanner confidence 0.5, below the
unchanged default gate of 0.6. Their detection is not proof that the default gate
would block that repository. Use report-only pilots to review finding quality and
choose policy with maintainers.

No target code or model was invoked for these scans. No upstream correction,
maintainer message, commit, deployment, publication or event submission was made
by this work. The existing Flow/video, public-hosting and release checks remain
pending. GitHub checks still belong to the pushed commit until a reviewed final
state is committed and pushed. Some files were staged externally during this
session; refresh staged content from the reviewed working tree, including the
byte-preservation settings and patch files, before committing.
