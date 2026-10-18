# Claim kinds and verdicts

Claims include CLI flags/commands, environment variables, HTTP endpoints,
literal settings/defaults, code symbols, file paths, package scripts, task targets
and project-owned shell command examples. Arbitrary behavioural prose is not
proved by static name matching. The optional model path is separately labeled.

| Verdict | Meaning | Rule |
| --- | --- | --- |
| missing | No supported declaration/path was found; inspect confidence and scope | DD001 |
| renamed | A replacement is proposed from similarity or history evidence | DD002 |
| default_changed | An explicit documented default disagrees with a declaration | DD003 |
| invalid_command | A project-owned command has unknown tokens | DD004 |
| unverifiable | Available static evidence cannot decide | DD005 |
| ok | The extracted claim has a matching static declaration | DD000 |

An `ok` result does not prove runtime success. Low-confidence missing findings are
diagnostics, not verified defects. Historical sections/changelogs are excluded.
Use [the benchmark](benchmark.md) for human-reviewed accuracy, not fixture scores.
