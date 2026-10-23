# Local maintainer pilot preparation — 23 October 2026

The owner supplied Nova and Prism.ai as pilot candidates. Both were cloned and
statically scanned at pinned revisions. No target dependencies were installed,
no target code or documentation examples were executed, and no model was called.
No maintainer was contacted by this workflow. Human labels, review time, CI
willingness and retained CI runs remain pending.

## Final frozen review packs

| Repository | Target revision | Docs / claims | Qualifying predictions | Human review rows | Unverifiable | Below-threshold drift | Warnings |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| [Nova](https://github.com/Dakshhhhh-ops/nova) | `392802dc0bf6d29ee5e60c4a5c47c6e337f7b14a` | 9 / 168 | 5 | 5 | 74 | 25 | 0 |
| [Prism.ai](https://github.com/harshbpathak/prism.ai) | `0a6cfbfe7f364894cdb3952f5377eeb17509ae37` | 21 / 198 | 30 | 25 | 29 | 64 | 264 |

Qualifying means at or above the unchanged configured confidence of 0.6. These
are scanner predictions, not confirmed defects. All 30 selected review rows are
blank. Precision and recall are null; acceptance and adoption are unverified.
Prism.ai's warnings remain visible; they are not counts of defects in Prism.ai.
In particular, TSX grammar recovery and unsupported patterns limit coverage.

Both final runs use seed 20261025 and the same archived detector source digest:
`f0aad91584a272225b6d30900ac16e09de9b46736f95f39cd1bb7920a7fd5a15`.
The scanner checkout was dirty at HEAD `774ecf4`; the archived source, actual
configuration and digest identify the detector, rather than that HEAD alone.
Neither target checkout is shallow. Acquisition used a maximum depth of 100;
the histories available at these pins fit inside that bound.

[Machine-readable observations](assets/quality/maintainer-pilot-2026-10-23.json)
include local scan timings excluding clone (about 2.30 seconds and 12.67 seconds).
These observations ran alongside test work and do not establish throughput,
whole-process memory, a performance target or an SLA.

Generated artifacts remain local under .docs-drift/maintainer-pilots/:

```text
2026-10-23-nova-r3/
2026-10-23-prism.ai-r3/
```

Each review pack contains the frozen scan, HTML report, source snapshot, pinned
documentation/code links, blank label sheet, blank feedback file and standalone
review instructions. See the [pilot workflow](MAINTAINER-PILOT.md). Share only
after reviewing the artifacts; nothing was sent automatically.

## Development failures and changes are preserved

Prism.ai's first scan failed on its valid pnpm root member `.`. Workspace
discovery now accepts root members and has a regression test. Earlier runs also
exposed URI schemes, scoped package names, protocol messages, import aliases,
function calls and file-creation instructions being mistaken for missing files.
The lexical/context rules were corrected with genuine missing-file controls.
Ambiguous references remain visible as unverifiable rather than being declared
correct. The earlier local outputs remain separate; no labels were backfilled.

These repositories informed detector development. They are not a held-out
accuracy benchmark, and fewer findings after these changes do not prove improved
precision or recall. The frozen 18 October benchmark and its human labels were
not modified. Planning documents, external dependency references, bare filenames
and unsupported framework semantics may still produce noise. A maintainer must
inspect the remaining predictions and decide whether report-only CI is useful.
