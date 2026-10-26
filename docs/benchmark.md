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

Human labeling instructions and blank columns are in `benchmark/labels_todo.md` in the source checkout. Raw local scan files are reproducible with the commands above.

## Agent-reviewed real-repository sample, October 2026

**These are agent-reviewed labels, not independent human labels.** An AI agent read the
documentation and the source for every finding below and labeled it true or false
positive; the same kind of agent also wrote the rule fixes. The numbers measure
progress on repositories that were looked at while tuning, so treat them as
optimistic. They do not replace the human-labeled benchmark above, and the
held-out claims below are only as held out as stated.

Method: shallow clones at the SHAs listed, `docs-drift scan --format json --all
--show-low-confidence`, then "reported" means verdict not `ok`/`unverifiable` with
confidence at or above the default `min_confidence` of 0.6. Precision is
true positives / reported findings. Recall was not measured exhaustively: a set of
known true positives found by hand (the recall guards) was checked in every round.

| Round | Build | Repositories | Reported | True | False | Precision |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 1 | `accuracy-round1` start | 12 popular repos (sample of up to 25 drift findings each, low-confidence included) | 277 reviewed | 1 | 276 | 0.4% |
| 2 | after round-1 rule fixes | same 12 | 10 | 0 | 10 | 0% |
| 2 | after round-1 rule fixes | 8 held out | 10 | 1 | 9 | 10% |
| 3 | this change | same 12 | 1 | 1 | 0 | 1/1 |
| 3 | this change | 8 held out in round 2 | 4 | 4 | 0 | 4/4 |
| 3 | this change | 45 hunt repos (see below) | 8 | 8 | 0 | 8/8 |

Round 3 on the 20 repositories of round 2 reports 5 findings, all true (Wilson 95%
interval 0.57–1.00; on the hunt set 8/8, 0.68–1.00). The small counts are the point
to note: the default threshold now reports very little, and most real drift found by
hand is still below it.

True positives reported by default in round 3 (file and line in the scanned
repository, then the documented text and the correct one; plain text because these
name other projects' files and options, not this one's):

| Repository | Location | Documented | Actually |
| --- | --- | --- | --- |
| pallets/click | docs/exceptions.md:110 | {class} role FileType | click.File |
| Textualize/rich-cli | README.md:61 | --line-number | --line-numbers |
| Textualize/rich-cli | README.md:180 | --p | -p |
| Textualize/rich-cli | README.md:196 | --force-terminal or -F | -F belongs to --text-full |
| mkdocs/mkdocs | docs/getting-started.md:161 | mkdocs/search_index.json | search/search_index.json |
| PyCQA/isort | docs/configuration/multi_line_output_modes.md:70 | -sl | --sl |
| Textualize/textual | CONTRIBUTING.md:104 | make test-snapshot | only test-snapshot-update exists |
| Textualize/textual | docs/how-to/package-with-hatch.md:94 | tests/\_\_init\_\_py | tests/\_\_init\_\_.py |
| aio-libs/aiohttp | THREAT_MODEL.md:482 | reader_c.pyx | reader_c.pxd |
| aio-libs/aiohttp | THREAT_MODEL.md:482 | reader_c.py | not in the checkout |
| locustio/locust | docs/faq.rst:88 | locust/templates/index.html | not in the checkout |
| pyinstaller/pyinstaller | tests/README.md:79 | test/requirements-library.txt | tests/requirements-libraries.txt |
| verdaccio/verdaccio | CONTRIBUTING.md:130 | /packages/ui-component | packages/ui-components |

Known true positives still below the default threshold (recall gaps): pdm's
reuse-env option (pdm's option registry is not indexed), scalene's json option and the
move of its cli and html options to the view subcommand, the pip-tools pip-sync
configuration table, pyinstaller's loader/rthooks path and its Travis and AppVeyor
links, commander.js CLIFORCE_COLOR, tldr-node-client's color option and colors.js,
vitest's BenchmarkRunner and its npm run example that lacks the separator before
project options, pm2's config.json and its unread AGENT variables, and locust's
custom route.

The hunt set (30 Python and 15 JavaScript/TypeScript repositories) was reviewed by two
other agents on the round-1 build and handed over as held-out data. It stopped being
fully held out once its false positives were read: an intermediate round-3 pass, run
while fixes were still being applied, reported 40 findings there of which about 35 were
false positives, and the reader-owned,
placeholder, gitignored and generated paths, npm scripts defined by the document
itself, triple-dash separators, PEP 517 hook names and naming-convention renames it showed
were fixed by general rules, not by repository names.

Repositories and SHAs. Round 2 and 3, earlier 12: pallets/click 2247b35e, pallets/flask
d086db85, fastapi/typer a5882ded, encode/httpx b5addb64, encode/uvicorn 9d81c12a,
psf/black 80db630a, simonw/datasette a8e13cc4, simonw/llm aebaca27, httpie/cli 5b604c37,
pypa/twine f536ac5d, vercel/serve 37b3a743, sindresorhus/np 5ca53e9a. Held out in round
2: pypa/pipx 9a49f195, Textualize/rich-cli 46f4d246, simonw/sqlite-utils 9c66bafa,
encode/starlette 8aa828ce, mkdocs/mkdocs 28625367, remy/nodemon 6812a747,
http-party/http-server 0d3b7bb5, tox-dev/tox d7ea3f58. Hunt set: Kludex/mangum 10824163,
PyCQA/flake8 567f9a96, PyCQA/isort c03ad5d6, Textualize/textual 5e5b7ef5, Unitech/pm2
34dec919, aio-libs/aiohttp ae3bc97b, asottile/pyupgrade 9c750258,
conventional-changelog/standard-version d70752c4, cookiecutter/cookiecutter c88fbe92,
copier-org/copier a00a46b2, developit/microbundle 9f56e06b, gaogaotiantian/viztracer
75a662d6, google/yapf 12005095, hhatto/autopep8 d6b5b334, jazzband/pip-tools b03df325,
jendrikseipp/vulture edd5806f, locustio/locust 7c3b5f5a, lukeed/sirv 1135207e,
pallets/jinja 5ef70112, pallets/werkzeug b73dce5a, pdm-project/pdm 301ca8c8,
plasma-umass/scalene 6d0081b4, pre-commit/pre-commit 8cb5845c, prettier/prettier
7664e368, pyinstaller/pyinstaller 44697216, pypa/build 4d37ed7e, pypa/hatch e7495de2,
python-semantic-release/python-semantic-release 60de8fca, release-it/release-it
45483990, semantic-release/semantic-release 04c19236, simonw/files-to-prompt 1b234ff6,
simonw/shot-scraper 63104b05, simonw/ttok df24f500, sindresorhus/del-cli 6276901d,
spotDL/spotify-downloader 58ced9af, streamlink/streamlink fcf06529, tiangolo/sqlmodel
462b59d9, tj/commander.js ba6d13dd, tldr-pages/tldr-node-client 02a5ce29,
typicode/json-server 89a34a44, verdaccio/verdaccio 377d8b15, vitest-dev/vitest
1a77c6cb, webpack/webpack-cli f7a54ad9, wntrblm/nox afa3983b, yargs/yargs 8021675d.
