# AWS deployment handoff

Prepared locally; **no AWS resources or public demo have been deployed**. Hosting
is deferred at the owner's request. No OpenRouter key is needed: the public web
service runs rule-only scans of public GitHub repositories without sign-in.

## Run the actual app locally

```sh
uv sync --locked --all-extras
uv run python -m docs_drift.web
```

Open http://127.0.0.1:8080. Paste a public GitHub repository URL and click Scan
repository. The service queues a disposable worker, reports progress, and opens
the HTML findings/graph when complete. A small repository is preferable; large
repositories can exceed the two-minute limit. Examples are precomputed and open
immediately. `/case-study` explains the real rich-cli finding and false positives;
`/built-with-voice` serves the supplied usage cards.

## Container contract

Use `Dockerfile.web`, not the CLI Dockerfile. It contains Git, the Python web
extra, the baked examples and static evidence; runs as uid 10001; listens on 8080;
and exposes `/healthz`. Supply a reviewed package version and source SHA:

```sh
docker build -f Dockerfile.web --build-arg PACKAGE_VERSION=0.1.0.dev0 --build-arg BUILD_SHA=YOUR_REVIEWED_SHA -t docs-drift:web .
docker run --rm -p 8080:8080 -e DOCS_DRIFT_ALLOWED_ORIGIN=http://127.0.0.1:8080 docs-drift:web
```

The development version above is for local validation, not a published release.
Container execution still needs verification on a Docker-capable host.

For an ECS/Fargate deployment, retain **one task and one Uvicorn worker**: queue,
rate limits and job state are process-local. Multiple replicas need a shared
queue/store or routing/state redesign. A task restart loses ephemeral live scans;
the baked reports remain. Allow time for existing jobs to finish during rollout.

| Setting | Value |
| --- | --- |
| Container port | 8080 |
| Health path | `/healthz` (must return 200) |
| `DOCS_DRIFT_ALLOWED_ORIGIN` | Exact final HTTPS origin, no trailing slash |
| `DOCS_DRIFT_DATA_DIR` | `/data`, writable by uid 10001 |
| `DOCS_DRIFT_MAX_CONCURRENT` | `2` maximum |
| `DOCS_DRIFT_LIVE_ENABLED` | `1` for judge-entered repos; `0` keeps only baked samples |
| `DOCS_DRIFT_BUILD_SHA` | Reviewed source SHA |
| Starting resources | 1 vCPU / 2 GiB; validate under expected scan load |

Terminate HTTPS at the load balancer, route to container:8080, and configure the
health check above. The task needs outbound HTTPS/DNS to fetch public GitHub
repositories. The app needs no AWS API permissions; the ECS execution role needs
only image-pull/log permissions appropriate to your registry/log setup. Create
roles and infrastructure in the later AWS deployment task, not by copying
placeholder ARNs. Consult [ECS guidance](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html).

## Verify after deployment

```sh
uv run python scripts/smoke.py https://YOUR_ACTUAL_DEMO_ORIGIN
```

Also open a private browser with no account, inspect the case, open a baked scan,
submit a small public repo, follow a finding and download JSON. Repeat on a phone.
Set the actual verified URL in README.md and SUBMISSION.md only after this passes.
The unrelated `docs-drift.vercel.app` address must not be used.

## PyPI

The PyPI JSON endpoint returned **404 on 22 October 2026**. Package publication
is separate from AWS hosting. Use the source checkout until the owner configures
the trusted publisher described in RELEASING.md and a release passes validation.
No PyPI API key should be pasted into chat. The app no longer advertises a
currently unavailable `pipx install docs-drift` command.
