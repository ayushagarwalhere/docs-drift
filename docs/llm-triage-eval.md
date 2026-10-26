# LLM triage evaluation

This page reports how much the optional `--llm-triage` pass (see
[model mode](llm-mode.md)) reduces false positives, and whether it hides real drift.
Short answer: it is safe on the cases we have, and it helps a little. With the
shipped prompt it kept every known true positive and removed about 5% of
false positives. It does not fix the rule problems behind most false positives.

**The labels are agent-labelled, not independent human labels.** An AI agent
reviewed each finding against the source and recorded a verdict and a cause.
Spot checks found them sensible, but nobody independently verified them, and the
true-positive set is small. Treat every number here as an estimate.

## Setup

- **Model:** `google/gemini-2.5-flash-lite` through OpenRouter, temperature 0,
  strict JSON schema, 8 findings per request. This is the default `triage_model`.
- **Round 1:** 277 findings sampled from rule output on 12 public repositories
  (click, flask, typer, httpx, uvicorn, black, datasette, llm, httpie, twine, serve,
  np): 276 false positives and 1 true positive (click `FileType`).
- **Extra round-1 true positives:** the two reviewed rich-cli bugs (README.md:61
  `--line-number`, README.md:180 `--p`) and the 14 seeded drifts in the synthetic
  fixtures (`py-cli`, `fastapi-svc`, `node-tool`). That makes 17 true positives.
- **Round 2:** findings from fresh scans with the round-2 rule fixes, on the same
  repositories plus held-out ones (pipx, mkdocs, tox, rich-cli). There are 19 false
  positives and 4 true positives (click `FileType`, rich-cli `--line-number` and
  `--p`, mkdocs `mkdocs/search_index.json`). One more labelled true positive
  (rich-cli README.md:196) has no rule finding, so there is nothing to triage.
- **Category split:** each false positive is either *not about this project* (an
  example the doc defines, another tool's flag or config, a placeholder, a user path,
  historical text, a built-in, program output) or a *project claim the rules
  mis-resolved* (index gaps, default parsing, wrong binding). Triage is designed to
  remove only the first kind. The second kind needs rule fixes.
  - Round 1: 119 not about this project, 157 project claims.
  - Round 2: 11 not about this project, 8 project claims.

Reproduce with `benchmark/triage_eval.py`. It needs the local sweep clones, scans
and label files, which are not committed. Prompt variants are in
`benchmark/triage_prompts/`.

```text
uv run python -m benchmark.triage_eval --sweep <dir> --labels <labels.json> \
  --fixtures --rich-cli <rich-cli clone> --prompt benchmark/triage_prompts/triage_v4.md \
  --mode record --budget 0.05
```

## Results

The selection rule was: keep 100% of true positives in both rounds, then remove
as many false positives as possible. "Not-project FPs" and "Project-claim FPs" count
the false positives removed in each group.

### Round 1

| Prompt | TPs kept | FPs removed | Not-project FPs | Project-claim FPs |
|---|---|---|---|---|
| v1 (baseline) | 17/17 | 17/276 (6.2%) | 11/119 | 6/157 |
| v2 (+ project identity, library-example rule) | 17/17 | 40/276 (14.5%) | 36/119 | 4/157 |
| v3 (+ "who owns it" steps, worked examples) | **15/17** | 50/276 (18.1%) | 41/119 | 9/157 |
| **v4 (shipped)** (v2 + files the project writes are its own) | 17/17 | 13/276 (4.7%) | 13/119 | 0/157 |

### Round 2

| Prompt | TPs kept | FPs removed | Not-project FPs | Project-claim FPs |
|---|---|---|---|---|
| v1 | 4/4 | 0/19 | 0/11 | 0/8 |
| v2 | **3/4** | 4/19 | 4/11 | 0/8 |
| v3 | 4/4 | 3/19 | 3/11 | 0/8 |
| **v4 (shipped)** | 4/4 | 1/19 | 1/11 | 0/8 |

Three v1 round-2 findings were never assessed because of a connection failure. Those
were the three httpie findings, two of which other variants removed. So v1's round-2 score is slightly understated.

### Why v4 ships

- **v3 is out.** It dismissed the click `FileType` bug and a seeded `--dry-run`
  flag removal as "not about the project".
- **v2 is out.** It dismissed the mkdocs `search_index.json` bug as a
  "user environment" path. The file is one mkdocs writes into the reader's build
  directory. Round 2 was labelled after v1 to v3 were written, so this is a genuine
  held-out failure.
- **v4 against v1.** v4 is v2 plus one rule: files the project itself creates or
  writes are its own claims. By raw count, v1 removes slightly more (17 + 0 against
  13 + 1). But 6 of v1's round-1 removals were project claims, some with
  self-contradicting reasons. For example, it dismissed httpx's proxy environment
  variables while calling them "part of the httpx project's configuration", and
  it dismissed `make html` and `llm embed-multi` as third-party commands. That is
  exactly how a real true positive would get hidden. v4 made no project-claim
  removals in round 1, and every one of its reasons is coherent. v4 was written
  after seeing the round-2 mkdocs failure, so its round-2 result is not held out.

### What v4 removes (round 1)

| Category | Removed |
|---|---|
| third_party_command | 3/14 |
| external_url_as_endpoint | 2/2 |
| intentional_example | 1/29 |
| shell_completion_placeholder | 1/4 |
| python_builtin | 1/3 |
| third_party_env_var | 1/2 |
| data_value_as_symbol, language_keyword, prose_word_as_flag, system_path_as_endpoint | 1 each (1/1) |
| third_party_schema_in_example, third_party_config, user/home/runtime paths, historical notes, all other not-project categories | 0 |
| all 157 project-claim false positives (index gaps, dynamic registration, argparse wrappers, …) | 0 |

In round 2, v4 removed one of the two httpie `httpie/debian.httpie.io` paths, which
point to a separate GitHub repository. It kept the other because the model reused
the first quote, and the capitalisation ("go" against "Go") failed verbatim
validation. It kept every default-value, rename and
binding defect, as designed.

The biggest missed groups are the 29 intentional examples and the 22
third-party config or schema blocks. The flash-lite model mostly answers "keep"
there. v2 removed 7/29 and 6/8, which shows the potential, but at the cost of a
true positive. Validation also rejected quotes:

| Prompt | Rejected `not_project_claim` answers (round 1) |
|---|---|
| v1 | 15 |
| v2 | 8 |
| v3 | 17 |
| v4 | 0 |

Rejections were for quoting only the name, joining lines, or paraphrasing. Each
one fell back to keeping the finding.

### Other model tried

`qwen/qwen3-235b-a22b-2507` with v4 kept all 4 round-2 true positives and removed
3/19 false positives: both httpie repo slugs and flask `tests/resources`. In
round 1, OpenRouter returned HTTP 402 for most requests, and responses took 19
to 31 s on average. Only 66 false positives were assessed (27 removed, 5 of them
project claims), and no true positives were. This is too incomplete to compare, so
it is not the default.

## Cost and latency

These figures are for Gemini 2.5 Flash Lite.

| Run | Requests | Tokens in / out | Cost | Latency per request |
|---|---|---|---|---|
| Full round 1 (298 findings) | 40–47 | ~155k / ~20k | $0.020–0.023 | mean 5–6 s, max 15 s |
| Full round 2 (23 findings) | 9 | ~19k / ~2k | ~$0.003 | similar |

A round-1 run took about 150–235 s wall time at concurrency 2, including 429
retries. All live evaluation, including the failed qwen attempts and debugging,
cost about $0.14 of OpenRouter credit.

## Failure modes seen and handled

- **Rate limits (HTTP 429) and connection errors.** The affected batch is kept
  with a warning. The client retries 429s four times with backoff.
- **Degenerate output.** Once, the model repeated `\n\t` inside a quote until it
  hit the token cap. Complete items are salvaged from the truncated JSON; the
  unfinished one keeps its finding.
- **Instruction-like documentation text.** Four simonw/llm excerpts mention
  "system prompts". Those findings are kept without being sent.
- **Invalid single items.** An unknown decision, for example, only affects that
  finding.

## Limitations

- The labels are agent-made, and the true-positive set is small (17 and 4, of which 14
  are synthetic fixture drift). One lost true positive in a larger set would change
  the conclusion.
- Prompts v2 to v4 were tuned on round-1 errors. Only v1 to v3 were blind to round
  2. The v4 numbers are optimistic.
- The gain is small. Most false positives are project claims the rules mis-resolve,
  and triage cannot fix those by design. Rule fixes matter far more than triage.
- Triage sees only documentation, not code. It cannot tell whether a "missing" name
  really is missing.
- Results depend on one small, cheap model. A larger model may remove more, but it
  was not measured completely here.
