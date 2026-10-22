# A real command failure, reproduced on 22 October 2026

This is an agent-assisted investigation performed during this build session,
not a story about the owner's past work. To render a repository README with line
numbers, we used rich-cli's own documented option at pinned revision
`46f4d2469097be395558768714a5f07ebccf1412`.

```console
rich README.md --line-number
Error: No such option '--line-number'.
```

Exit status: **2**. The corrected `rich README.md --line-numbers` exited **0**.
The isolated runtime used Python 3.13.7, rich-cli 1.8.1 and Click 8.5.0.
`PYTHONIOENCODING=utf-8` was set for all four recorded checks: without it, the
correct command hit an unrelated Windows console encoding error on an emoji.

## Follow the evidence

1. [README line 61](https://github.com/Textualize/rich-cli/blob/46f4d2469097be395558768714a5f07ebccf1412/README.md#L61)
   documents `--line-number`.
2. [Click declaration at line 358](https://github.com/Textualize/rich-cli/blob/46f4d2469097be395558768714a5f07ebccf1412/src/rich_cli/__main__.py#L358)
   accepts `--line-numbers` and `-n`.
3. [Documentation commit 567f6c84](https://github.com/Textualize/rich-cli/commit/567f6c848ee5efa8eef9d6c96ffa3c23b802da4f)
   introduced the incorrect wording. The plural source option predates it
   (`c180c36f`, per Git blame). This was a documentation typo, not a proven rename.

docs-drift locates the missing option at README:61. Its `introduced_by` field is
null for this finding; the relevant documentation commit was established by
manual Git inspection, not automatic breaking-change attribution. Do not show
an invented commit edge in the scanner graph.

Reproduce from a trusted checkout (runtime commands are deliberately separate
from docs-drift's static scan, which never executes target code):

```sh
git clone https://github.com/Textualize/rich-cli.git rich-cli
git -C rich-cli checkout 46f4d2469097be395558768714a5f07ebccf1412
docs-drift scan --repo rich-cli --format json --output rich-cli.json
docs-drift index --repo rich-cli --find=line-number --no-color
git -C rich-cli blame -L 61,61 -- README.md
git -C rich-cli show 567f6c848ee5efa8eef9d6c96ffa3c23b802da4f -- README.md
```

The index lookup ranks `--line-numbers` first (90.0 fuzzy-search score) at
`src/rich_cli/__main__.py:357`, the decorator opening above the declaration on
line 358. That score is a search similarity, not a probability of correctness.

## Small review sample, including false positives

Selection: all four non-ok/non-unverifiable findings in the previously baked
rich-cli report, not a random or representative sample. Reviewer: Codex,
using source inspection and actual command execution. **No independent human
labels or real-world precision/recall claim.** The frozen benchmark is unchanged.

| Doc line / token | Saved verdict | Review | Check |
| --- | --- | --- | --- |
| 61 / `--line-number` | missing | confirmed defect | exit 2; plural option at source:358 exits 0 |
| 180 / `--p` | missing | confirmed defect | exit 2; `-p` at source:251 exits 0 |
| 89 / `.md` | missing path | false positive | extension prose; source:514 detects `.md` |
| 116 / `.json` | missing path | false positive | extension prose; source:516 detects `.json` |

Saved sample: **2 confirmed defects, 2 false positives, n=4**. A fresh scan with
this checkout reports only the two option findings; both extension false positives
are absent. This difference is not evidence of overall accuracy or recall.
Raw runtime observations and both scans are packaged with the public sample.

## Proposed upstream correction (not submitted)

```diff
- Add the `--line-number` or `-n` switch
+ Add the `--line-numbers` or `-n` switch
- If you add the `--print` or `--p` option
+ If you add the `--print` or `-p` option
```

The README's `--port` / `--listen` example remains a synthetic fixture. It is not
part of this review or the real incident.
