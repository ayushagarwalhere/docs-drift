# One-claim local demo

This is a synthetic fixture, precomputed by the existing static scan worker.
It demonstrates an inspectable workflow, not real-world detection accuracy.
The bundled JSON opens without GitHub access, credentials, or fixture generation.

From the repository root, with Python 3.11+, Git, and uv installed, run in PowerShell:

```powershell
uv sync --locked --all-extras
$env:DOCS_DRIFT_ALLOWED_ORIGIN = "http://127.0.0.1:8080"
$env:DOCS_DRIFT_LIVE_ENABLED = "0"
uv run python -m docs_drift.web
```

Open <http://127.0.0.1:8080> and select **Try one stale claim (synthetic fixture)**,
or open <http://127.0.0.1:8080/scan/example-judge> directly.

1. Read the single stale claim at README.md:3: use `--port`.
2. Expand **Evidence**. Inspect the current argparse source and the Git diff
   changing `--port` to `--listen`. The finding identifies commit `66f495f`
   ("Rename port to listen without updating README"). This is a local fixture
   commit, so it has no GitHub link.
3. Review the proposed replacement `--listen`. Nothing is applied automatically.
   **Download review** and **Download JSON** preserve the finding and evidence.

For optional live public GitHub scans, stop the server, set
`$env:DOCS_DRIFT_LIVE_ENABLED = "1"`, and restart. Live scans need outbound GitHub
HTTPS access, but no token. Private repositories are not supported by this demo.
Invalid URLs, paused service, queue limits, and worker failures have explicit
errors; completed reports are unavailable for failed jobs. The sample remains usable.

## Reproduce and inspect the fixture

Choose a fresh output directory; the builder refuses to overwrite an existing fixture:

```powershell
uv run python -m scripts.build_judge_demo --output .docs-drift/judge-reproduction
git -C .docs-drift/judge-reproduction/judge-fixture log --oneline
git -C .docs-drift/judge-reproduction/judge-fixture show HEAD -- cli.py
uv run docs-drift scan --repo .docs-drift/judge-reproduction/judge-fixture
```

The builder uses the existing deterministic fixture utilities and production static
worker. `--bake` additionally refreshes the bundled JSON and manifest. The two Git
commits are deterministic; scan timestamps and timings vary. Changing README's
`--port` to `--listen` removes the finding on rescan (covered by the focused test).

```powershell
uv run pytest tests/test_judge_demo.py tests/test_web.py tests/test_web_boundaries.py tests/test_git_process.py tests/test_public_security.py tests/test_ci_and_worker.py -q
```

## Execution boundary and remaining work

The scanner parses repository files as data; it does not import them, install
dependencies, run tests, or execute package scripts. Public workers use server-owned
configuration with model calls disabled. Git fetches use isolated configuration,
disabled hooks and fsmonitor, no recursive submodules, and restricted protocols;
history reads disable external diff and textconv. Tests plant Python execution
payloads, a package install script, and a Git hook, and verify scanning invokes
only Git and leaves no execution marker. Existing tests cover unsafe URLs, input
limits, output escaping, timeouts, and cleanup. This verifies the intended execution
boundary; it is not a sandbox proof against vulnerabilities in Git or parsers.

Local use and the bundled sample require no owner approval or service credentials.
Initial dependency installation needs package-network access. Public hosting is
still deferred by the owner: deployment needs approval, a hosting account and its
credentials, a final origin, and post-deployment smoke checks. No deployment or
public URL is claimed here. PyPI publication, upstream documentation changes,
video recording/upload, and challenge submission remain owner actions; none is
needed for this local demo. See [deployment](deploy.md).
