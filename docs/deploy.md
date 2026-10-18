# Public demo deployment

Status: local implementation and deployment preparation. No hosting account,
resource, token, domain or public deployment has been created. Approve a host
before running provisioning commands. GitHub repository/Action/extension links
must be set to the actual project repository before launch.

## Host choice (checked 18 October 2026)

Recommend one **always-on Fly Machine, Singapore, 2 GB RAM**, one Uvicorn process,
at most two disposable scan workers. Its background CPU stays available after
POST returns. Keep a roughly **$15/month compute allowance**, plus traffic/storage;
the exact Singapore quote must be reviewed before provisioning. Current published
US baseline is $2.19 for 256 MB plus $6/GB additional RAM: about $12.69 for one
shared CPU and 2 GB, rather than the older prices found in many comparisons.
[Fly pricing](https://docs.fly.io/about/pricing),
[October 2026 update](https://fly.io/pricing-update/).

| Host | Idle/cold start | Free/cheap resources | Git and disk | India-friendly region; GitHub deploy |
| --- | --- | --- | --- | --- |
| Render | Free sleeps after 15 idle minutes; waking can take about a minute. Paid avoids idle sleep. | Free 512 MB/0.1 CPU, 750 hours/month; 2 GB paid compute listed at $25/month. Free outbound/build limits matter for clones. | Install Git in this Docker image. Free filesystem ephemeral; paid disks available. | Singapore; GitHub Docker auto-deploy or image deploy. |
| Fly.io | Auto-stop disabled in supplied config: no idle wake. Deploy/restart boot latency remains unmeasured. | Pay as you go; no permanent free allocation for new accounts. Choose RAM/CPU explicitly. | Git in image; root disk ephemeral, optional volume $0.15/GB/month. | Singapore; provided GitHub Action builds a SHA image and deploys it. |
| Railway | Leave optional Serverless sleeping off; no idle wake. Actual restart time unmeasured. | Free $1/month credit after trial, up to 0.5 GB RAM/1 CPU; Hobby $5/month minimum credited toward metered usage, not unlimited compute. | Git in image; ephemeral disk plus persistent volumes by plan. | Singapore; GitHub repo/Dockerfile auto-deploy. |
| Hugging Face Docker Spaces | CPU Basic can sleep; rebuild/wake depends on image. No app-specific timing measured. | CPU Basic: 2 vCPU, 16 GB RAM, 50 GB ephemeral disk, no hourly charge; **new Docker compute Spaces now require a paid subscription**, so this is not a free account option. | Git installed in image; persistent storage costs extra. | No selectable India region verified; GitHub needs a sync/push workflow to the Space Git repo. |
| Google Cloud Run | Scales to zero by default; variable container boot. Min instances removes idle scale-to-zero. | Request-based free allowance includes 2 million requests, 180k vCPU-seconds, 360k GiB-seconds/month; always-allocated background CPU uses different billing/allowances. | Git in image; writable filesystem is memory backed/ephemeral, persistent state needs an external store. | Mumbai/Delhi; Cloud Build GitHub integration or registry deploy Action. |

Sources: [Render free](https://render.com/docs/free),
[Render pricing](https://render.com/pricing),
[Railway plans](https://docs.railway.com/pricing/plans),
[Railway regions](https://docs.railway.com/deployments/regions),
[Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview),
[Cloud Run pricing](https://cloud.google.com/run/pricing),
[Cloud Run billing](https://docs.cloud.google.com/run/docs/configuring/billing-settings),
[Cloud Run regions](https://cloud.google.com/run/docs/locations).
These are provider policies/specifications, not measured cross-provider benchmarks.
Prices exclude taxes and extra network usage.

Cloud Run request-based CPU is unsuitable for this in-process queue: returning
202 must not freeze the scan. It needs instance-based billing, min/max instances
one, and enough RAM, or a redesigned external queue. Render paid is a simpler
GitHub UI alternative at higher baseline cost. Fly requires more CLI setup and
metered-cost monitoring; a single Machine is not highly available.

## Local run and container

```sh
uv sync --locked --extra web
DOCS_DRIFT_ALLOWED_ORIGIN=http://127.0.0.1:8080 uv run python -m docs_drift.web
docker build -f Dockerfile.web --build-arg BUILD_SHA=$(git rev-parse HEAD) -t docs-drift-web:local .
docker run --rm -p 8080:8080 --memory=2g --cpus=1 --pids-limit=128 --cap-drop=ALL --security-opt=no-new-privileges docs-drift-web:local
python scripts/smoke.py http://127.0.0.1:8080
```

PowerShell: set `$env:DOCS_DRIFT_ALLOWED_ORIGIN` before running Python. For the
screenshots the local server runs at http://127.0.0.1:8000. Public origin must
match exactly, including scheme/port, without a trailing slash.

The slim multi-stage image contains Git/CA roots, the locked web dependency set,
non-root UID 10001 and baked example JSON. It runs one Uvicorn worker because the
queue, rate limit, counters and cache coordination are process-local. Never use
multiple replicas/workers without replacing QueueBackend and shared storage/rate
limiting. The Linux child has 512 MB address-space and CPU limits; a container/VM
memory limit is still required. Windows local development lacks RLIMIT support.

## Environment

| Variable | Default and purpose |
| --- | --- |
| `DOCS_DRIFT_DATA_DIR` | `.docs-drift/web-data` locally, `/data` in image; writable results and temporary clones. Live results expire after 24h; disk result cap 50/200 MB, memory LRU 50. |
| `DOCS_DRIFT_MAX_CONCURRENT` | `2`, accepts 1–2. Queue cap 32; each clone at most 200 MB/20k tracked files. |
| `DOCS_DRIFT_ALLOWED_ORIGIN` | `http://127.0.0.1:8000`; exact frontend origin for CORS and POST Origin checks. Falls back to `RENDER_EXTERNAL_URL` if set. |
| `DOCS_DRIFT_RATE_LIMIT` | `5` scans per IP per 600 seconds, accepts 1–100. Use a trusted proxy configuration for real client addresses. |
| `DOCS_DRIFT_CLONE_TIMEOUT` | `60` seconds maximum, configurable downward. |
| `DOCS_DRIFT_SCAN_TIMEOUT` | `120` seconds maximum, configurable downward; worker cancellation/cleanup follows. |
| `DOCS_DRIFT_HISTORY_DAYS` | `365`, configurable downward. Date-limited partial fetch; inactive projects fall back to the commit depth below. |
| `DOCS_DRIFT_HISTORY_COMMITS` | `2000`, configurable downward; history lookup traversal/depth cap. |
| `DOCS_DRIFT_LIVE_ENABLED` | `1`; `0` permanently disables submissions while examples stay available. Three clone failures also pause live scans for 5 minutes automatically. |
| `DOCS_DRIFT_BUILD_SHA` | `development`; image build arg sets deployed commit shown in footer/healthz. Falls back to `RENDER_GIT_COMMIT`. |
| `DOCS_DRIFT_PROJECT_URL` | Placeholder `https://github.com/docs-drift/docs-drift`; **replace before release** with the actual repo for navigation/integration links. |
| `PORT` | `8080`; Uvicorn bind port, healthcheck/container expect 8080. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1`; only trust a known reverse proxy. Supplied Fly config trusts its front proxy; direct instance ports must not be publicly exposed. |
| `GITHUB_TOKEN` | Optional in the deployment environment; **unused** by this implementation, which uses only anonymous public Git. It is never passed to workers. No API token is needed. |

There is no public model-enable flag or endpoint. Scans always use server-owned
rule configuration; repository model/config settings cannot enable billing.
Structured scan logs omit IPs, source text, author identities and exception
tracebacks. `/api/stats` reports completed non-cached scans/stale statements and
failures since this process started; it does not pretend restarts retain totals.

## Approval and release setup

After host confirmation, the owner signs in and creates an app (no signup by the
agent). Set the real app/origin in `fly.toml`, project URL in the host environment,
and an optional 1 GB `/data` volume for live permalink survival across deploys.
Without a volume examples always work, but live permalinks may disappear on a
restart before their normal 24-hour expiry. Keep exactly one Machine with
`flyctl scale count 1 --app APP` and verify its selected region/RAM/price.

Configure repository variables `FLY_APP`, `DOCS_DRIFT_BASE_URL`, and, only after
approval, `DOCS_DRIFT_DEPLOY_ENABLED=true`. Set an app-scoped Fly deploy token in
the repository secret `FLY_API_TOKEN`; never commit it. Restrict the production
environment to main. Successful CI push runs on main trigger the deploy workflow;
fork PR runs cannot deploy or obtain the token. Images use the tested commit SHA,
and `scripts/smoke.py` runs against the public URL after deploying.

Set up a PyPI trusted publisher for this repository, workflow `.github/workflows/release.yml`,
environments `testpypi` and `pypi`, and protect both environments. Follow the
exact owner setup and separate publish gates in the repository's RELEASING.md.
The release workflow uses OIDC,
not a stored PyPI token. No publisher registration or release was performed here.

## Judging uptime, monitoring and rollback

The scheduled monitor is enabled only by the repository variable above and only
acts from **2026-10-18 through 2026-11-06 inclusive (UTC)**, every ten minutes.
GitHub schedules are best effort and may be delayed. It probes `/healthz` and the
baked py-cli result twice, ten seconds apart, then opens one deduplicated issue
if both attempts fail. Subscribe to repository issue notifications to receive
email; the workflow cannot guarantee account notification settings. Manual runs
can test monitoring outside the date window.

The end of monitoring does not stop the paid Machine or billing. The owner can
stop/delete the approved resources after results are announced.

Use paid always-on hosting and actual health monitoring. Fly explicitly supports
disabling auto-stop; no anti-sleep traffic is needed. We have not established that
artificially keeping a Render Free/Spaces free process awake is permitted, and
do not recommend using pings to bypass provider idle policies.
[Fly auto-stop configuration](https://fly.io/docs/launch/autostop-autostart/).

View logs with `flyctl logs --app APP`; check `flyctl status --app APP` and
`/healthz` SHA. Roll back with
`flyctl deploy --app APP --image registry.fly.io/APP:PREVIOUS_SHA`. Then rerun smoke.py. Keep the
previous successful image tag. If GitHub cloning fails, the automatic circuit
breaker leaves all baked examples and report routes available; set
`DOCS_DRIFT_LIVE_ENABLED=0` for a longer incident and redeploy.

Deployment smoke output cannot exist until a host is approved and a public URL
exists. Docker image execution is also pending: Docker CLI is installed locally
but its daemon is unavailable. Local browser, API and scanner checks are recorded
in `docs/WEB-VERIFICATION.md`.
