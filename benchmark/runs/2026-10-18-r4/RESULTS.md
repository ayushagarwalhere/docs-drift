# Results — 2026-10-18 / 2026-10-18-r4

Scanner: docs-drift 0.1.0; commit `d6e96eb92bde9632b6f0faf69bcbbb508ef57717`; dirty=True.
Actual scanner source SHA-256: `da3599d3d72ac498d33d3da3114ee7dbc3c7050846ab74b9c61fcd1558a08673` (source archive retained).

Human labeler: **not yet supplied**. Pending human labels: **511**.
No AI labels were used. Blank labels are never counted as correct or incorrect.

Commands: `python -m benchmark.acquire --clone`; `python -m benchmark.run scan --run 2026-10-18-r4 --finding-seed 20261026 --recall-seed 20261027 --timeout-seconds 1200`; `python -m benchmark.run labels --run 2026-10-18-r4`; `python -m benchmark.compute --run 2026-10-18-r4`.

## Precision (human labels; Wilson 95% intervals)

| Group | True / labeled | Selected | Precision | Wilson 95% |
| --- | ---: | ---: | ---: | --- |
| repo:django-oscar/django-oscar | 0/0 | 25 | pending | pending |
| repo:dubinc/dub | 0/0 | 18 | pending | pending |
| repo:encode/httpx | 0/0 | 12 | pending | pending |
| repo:expressjs/express | 0/0 | 0 | pending | pending |
| repo:fastapi/fastapi-cli | 0/0 | 0 | pending | pending |
| repo:fastapi/full-stack-fastapi-template | 0/0 | 14 | pending | pending |
| repo:pallets/flask | 0/0 | 25 | pending | pending |
| repo:pre-commit/pre-commit | 0/0 | 0 | pending | pending |
| repo:simonw/sqlite-utils | 0/0 | 25 | pending | pending |
| repo:tj/commander.js | 0/0 | 2 | pending | pending |
| repo:withastro/astro | 0/0 | 25 | pending | pending |
| repo:yargs/yargs | 0/0 | 5 | pending | pending |
| verdict:default_changed | 0/0 | 0 | pending | pending |
| verdict:invalid_command | 0/0 | 20 | pending | pending |
| verdict:missing | 0/0 | 120 | pending | pending |
| verdict:renamed | 0/0 | 11 | pending | pending |

## Recall audit

Weighted audit recall: pending / not estimable.
Stratified population weights are recorded in samples.json. This is not exhaustive recall; no binomial interval is assigned to the weighted ratio.
The audit excludes unverifiable and low-confidence predicted findings.

## Coverage and runtime

| Repo | Status | Claims | Unverifiable / findings | Share | Scan seconds |
| --- | --- | ---: | --- | ---: | ---: |
| pre-commit/pre-commit | done | 6 | 1/6 | 0.166667 | 5.038193899963517 |
| simonw/sqlite-utils | done | 766 | 6/766 | 0.007833 | 9.627685700019356 |
| fastapi/fastapi-cli | done | 1 | 0/1 | 0.000000 | 4.2751385000301525 |
| fastapi/full-stack-fastapi-template | done | 110 | 16/110 | 0.145455 | 7.668038200004958 |
| pallets/flask | done | 299 | 2/299 | 0.006689 | 14.929833100002725 |
| django-oscar/django-oscar | done | 232 | 9/232 | 0.038793 | 30.731740200018976 |
| tj/commander.js | done | 61 | 1/61 | 0.016393 | 6.844292499998119 |
| yargs/yargs | done | 143 | 0/143 | 0.000000 | 9.00303789996542 |
| expressjs/express | done | 3 | 1/3 | 0.333333 | 7.672286399989389 |
| dubinc/dub | done | 101 | 34/101 | 0.336634 | 41.30136300000595 |
| encode/httpx | done | 94 | 0/94 | 0.000000 | 13.690128299989738 |
| withastro/astro | done | 305 | 59/305 | 0.193443 | 531.7667156999814 |

## What went wrong

Human false-positive patterns are pending labeling; this does not mean there were no false positives.

Infrastructure/parser failures and raw warnings remain in status.json, per-repository logs and measurements.
Repository-level sampling is uniform within each repo; pooled verdict precision is descriptive and can be composition-biased.
A dirty source archive is required to reproduce this run; the scanner commit alone is insufficient.
