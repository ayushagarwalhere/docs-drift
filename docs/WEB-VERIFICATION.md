# Web verification history ? 18 October 2026

The service is running locally at **http://127.0.0.1:8000**. No external host,
account or resource has been created. Public deployment and its post-deploy smoke
test await approval. Docker CLI exists but its daemon is unavailable, so the
production image has not been executed. Linux behavior awaits CI/image checks;
the local test machine is Windows with Python 3.13.

## Browser checks

Actual **Lighthouse 13.5.0 accessibility scores: landing 100/100, results 100/100**.
Audits used installed Edge via a Playwright-launched CDP browser and Lighthouse's
mobile defaults. This is an automated accessibility score, not a complete manual
assistive-technology audit. Reproduce with `python scripts/check_web.py`.

Both pages were captured at 1440×1050 and **360×800**, in light/dark mode, with
document scroll width checked against viewport width. Graph follows the finding
list on phones. The result is rendered as a complete document after polling,
with a reserved progress panel during the scan; it does not insert/reflow a
report piecemeal. No CLS performance score is claimed from the accessibility-only
audit. Existing report checks cover keyboard selection, evidence expansion,
filters, insights, themes and graph interaction.

Measured uncompressed document + required local CSS/JS, excluding Cytoscape:

| Page | Bytes |
| --- | ---: |
| Landing | 20,225 |
| py-cli result | 26,187 |

The report uses the same partials as offline HTML, with a separate vendored
Cytoscape request in web mode. Large web reports cap visible findings/excerpts
and graph nodes; JSON retains the complete result within the output size cap.

Screenshots:

| Page | Desktop | Phone |
| --- | --- | --- |
| Landing, light | [image](../examples/web-landing-desktop-light.png) | [image](../examples/web-landing-phone-light.png) |
| Report, light | [image](../examples/web-result-desktop-light.png) | [image](../examples/web-result-phone-light.png) |
| Landing, dark | [image](../examples/web-landing-desktop-dark.png) | [image](../examples/web-landing-phone-dark.png) |
| Report, dark | [image](../examples/web-result-desktop-dark.png) | [image](../examples/web-result-phone-dark.png) |

## Public repository scans and manual review

Both repositories completed scans through the live local HTTP API. Initial
uncached production-worker runs measured:

| Repository/snapshot | Checkout seconds | Checkout disk bytes | Total seconds |
| --- | ---: | ---: | ---: |
| rich-cli `46f4d246` | 6.18 | 80,765 | 11.36 |
| files-to-prompt historical `ae06f203` | 6.06 | 45,212 | 28.54 |

The HTTP files-to-prompt run took 33.47 seconds (6.25-second checkout). These are
local observations, not hosted performance promises. The `download_bytes` metric
is the sampled checkout disk footprint (Git objects plus checked-out files),
**not wire transfer bytes**; history may fetch additional blobs. A cache hit still
resolves the current remote SHA, then stops before checkout. Baked example links
need no network access to GitHub and return immediately.

Manually verified statements:

- rich-cli README line 61 says `--line-number`, source declares `--line-numbers`;
  line 180 says `--p`, source declares `--print`/`-p`.
  [README](https://github.com/Textualize/rich-cli/blob/46f4d2469097be395558768714a5f07ebccf1412/README.md#L61),
  [source](https://github.com/Textualize/rich-cli/blob/46f4d2469097be395558768714a5f07ebccf1412/src/rich_cli/__main__.py#L358).
- files-to-prompt README line 55 says `c/--cxml` instead of `-c/--cxml` at the
  pinned historical commit. This was fixed upstream; the example is explicitly
  historical and does not allege the current project still has the mistake.
  [historical README](https://github.com/simonw/files-to-prompt/blob/ae06f2039f3a1dc1c9f3b4657fb51459490768f2/README.md#L55),
  [source](https://github.com/simonw/files-to-prompt/blob/ae06f2039f3a1dc1c9f3b4657fb51459490768f2/files_to_prompt/cli.py#L141),
  [upstream fix](https://github.com/simonw/files-to-prompt/commit/f7cf729babc92604b9cd06c3d9cad32daff0f276).

Other static findings in these reports are not manually certified. In particular,
sample paths, extension shorthand, and package entrypoint resolution may produce
low-confidence missing findings. Notes are included in each example's report and
manifest. No model responses were used. Four fixture examples are synthetic and
identified separately from public repositories.

Rebuild these exact public snapshots with:

```sh
python scripts/build_web_examples.py --real https://github.com/textualize/rich-cli/tree/46f4d2469097be395558768714a5f07ebccf1412 https://github.com/simonw/files-to-prompt/tree/ae06f2039f3a1dc1c9f3b4657fb51459490768f2
```

## API/security checks

`scripts/smoke.py http://127.0.0.1:8000` printed:

```text
PASS landing and security headers
PASS cached py-cli fixture
PASS live public scan, HTML and schema-v1 JSON
```

Tests cover URL/ref injection and SSRF inputs, queued/running/done/empty/error
templates, escaped XSS through web routes, strict Origin/body/rate limits,
timeouts, cache stop protocol, disk watchdog cancellation, immutable examples,
graph bounds, 24-hour expiry, failure cleanup and read-only Windows packfiles.
Actual HTTP scan work directories were checked empty after cleanup was fixed.

`pip-audit --skip-editable` found **no known vulnerabilities** in the installed
environment. **471 tests passed; coverage 81.96%.** Ruff, formatting, locked
dependency consistency and strict mypy on `src` pass. The wheel was built and inspected to verify it
contains the baked examples, templates, shared partials and vendored assets. GitHub workflows parse as YAML but have not executed
remotely. Repository creation, real project URL, host setup, secret/environment
configuration, container run, and deployed-URL smoke testing remain pending.
