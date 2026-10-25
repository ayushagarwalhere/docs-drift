# Public demo deployment

The demo runs as one Docker container on an AWS EC2 instance, behind an HTTPS
reverse proxy, at https://13-211-179-95.sslip.io. `/healthz` reports the deployed
commit SHA.

## Shape

- One container from `Dockerfile.web`, one Uvicorn worker, at most two scan
  workers. Queue, rate limits and job state are in-process, so run exactly one
  container.
- The app container is named `app` and listens on 8080 on the Docker network
  `web`, with no host port. A Caddy container on the same network terminates TLS
  (automatic Let's Encrypt) and forwards to it:
  `caddy reverse-proxy --from <host> --to app:8080`.
- A named Docker volume holds `/data` (live results expire after 24 hours; the
  baked examples ship in the image).
- The host needs outbound HTTPS to clone public GitHub repositories. It needs no
  AWS API permissions.

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
| `FORWARDED_ALLOW_IPS` | `127.0.0.1`; only trust a known reverse proxy. Set it to the reverse proxy address; the container port must not be publicly exposed. |
| `GITHUB_TOKEN` | Optional in the deployment environment; **unused** by this implementation, which uses only anonymous public Git. It is never passed to workers. No API token is needed. |

There is no public model-enable flag or endpoint. Scans always use server-owned
rule configuration; repository model/config settings cannot enable billing.
Structured scan logs omit IPs, source text, author identities and exception
tracebacks. `/api/stats` reports completed non-cached scans/stale statements and
failures since this process started; it does not pretend restarts retain totals.

## Deploying a new version

`deploy/ec2/redeploy.sh` runs on the server. It checks out the requested commit,
builds `Dockerfile.web`, replaces the container, waits until `/healthz` reports
the new commit, and rolls back to the previous image if it does not.

```text
ssh <user>@<host>
cd /opt/docs-drift            # the server's checkout of this repository
DEPLOY_SHA=origin/main ./deploy/ec2/redeploy.sh
```

Container settings (origin, project URL, limits) go in `/etc/docs-drift.env`,
one VAR=value per line, so they survive redeploys.

## Automatic deploys

`.github/workflows/deploy-web.yml` runs the same script over SSH after CI
passes on main, then checks that the public `/healthz` reports the tested commit
and runs `scripts/smoke.py` against the public URL. It can also be started by
hand with a commit SHA.

Repository settings (Settings → Secrets and variables → Actions):

| Kind | Name | Value |
| --- | --- | --- |
| Variable | DOCS_DRIFT_DEPLOY_ENABLED | true |
| Variable | DOCS_DRIFT_BASE_URL | public origin, no trailing slash |
| Variable | EC2_HOST | instance public IP or DNS name |
| Variable | EC2_USER | SSH user that can run docker |
| Variable | EC2_APP_DIR | checkout path on the server (default /opt/docs-drift) |
| Secret | EC2_SSH_KEY | private key for that user (a deploy-only key is best) |
| Secret | EC2_KNOWN_HOSTS | output of ssh-keyscan for the host |

Set DOCS_DRIFT_DEPLOY_FREEZE to true to pause deploys (for example during
judging). Fork pull requests never reach this workflow or its secrets.

## Logs and rollback

On the server: `docker logs -f app` and `docker ps`; the public `/healthz`
shows the running commit. To roll back, redeploy a previous commit with
`DEPLOY_SHA=<sha> ./deploy/ec2/redeploy.sh`; earlier images are kept for a week.
If GitHub cloning fails, the automatic circuit breaker keeps the baked examples
and reports available; set DOCS_DRIFT_LIVE_ENABLED=0 in `/etc/docs-drift.env`
and redeploy for a longer incident.

## Monitoring

`.github/workflows/monitor-web.yml` probes `/healthz` and a baked report every
ten minutes while deploys are enabled, and opens one deduplicated issue if both
attempts fail.

## PyPI

Releases use a PyPI trusted publisher (OIDC) for `.github/workflows/release.yml`
with protected `testpypi` and `pypi` environments. See RELEASING.md.
