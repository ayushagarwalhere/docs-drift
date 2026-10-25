# docs-drift GitHub App (self-hosted)

The GitHub App reviews pull requests without any workflow YAML. On every push to a
PR it fetches the head and base commits with a short-lived installation token,
scans them statically (no repository code is executed), and publishes through the
same core as the [Action](github-integration.md):

- a **Check Run** named `docs-drift` (in progress while scanning, then success or
  failure from the gate, with annotations);
- the **sticky summary comment**, edited in place;
- **inline suggested changes** on PR diff lines when the fix is certain.

It also handles `/docs-drift recheck` comments from repository owners, members and
collaborators. The App is part of the web service in this repository
(`docs_drift.web`); the webhook route is mounted only when the three App
environment variables below are set, so the public demo is unchanged otherwise.

## Permissions and events

| Permission | Access | Why |
| --- | --- | --- |
| Checks | Read and write | Create and complete the `docs-drift` Check Run |
| Pull requests | Read and write | Read PR files and diffs; post the summary and review comments |
| Issues | Read and write | PR conversation comments use the Issues API |
| Contents | Read-only | Fetch the head and base commits |
| Metadata | Read-only | Mandatory for every App |

Subscribed events: `pull_request` (opened, synchronize, reopened, ready for
review; drafts are skipped) and `issue_comment` (for `/docs-drift recheck`).
`installation` and `installation_repositories` events are delivered to every App
and are only logged.

## Register the App in two clicks (manifest flow)

[deploy/github-app-manifest.json](https://github.com/ayushagarwalhere/docs-drift/blob/main/deploy/github-app-manifest.json)
is the manifest for the production host, `https://13-211-179-95.sslip.io`. The
helper builds the same manifest (for another URL with `--url`), serves a one-button
page on `127.0.0.1`, receives GitHub's redirect, exchanges the one-time code through
GitHub's manifest conversion API and prints the three environment lines to add on
the server. Nothing is written to disk or to the repository. Install the CLI with
the `github-app` extra first (for example from a checkout: `uv sync --extra github-app`).

```text
docs-drift github app-manifest --serve
# optionally: --org <organization>, --url https://<another-host>
```

1. Open the printed local URL and click **Create GitHub App**.
2. Review the permissions on GitHub and confirm. GitHub redirects back to the
   helper, which prints the environment lines and an installation link.

If you prefer to paste the code yourself (for example, from another machine), run
`docs-drift github app-manifest` to print the JSON, submit it through GitHub's
manifest form, and exchange the `code` from the redirect URL within one hour:

```text
docs-drift github app-exchange <code>
```

The printed private key is base64-encoded PEM so it fits on one line. Clear your
terminal scrollback afterwards. If GitHub returns no webhook secret, the helper
generates one and tells you to paste it under App settings → Webhook secret.

## Deploy on the EC2 Docker host

The App runs inside the existing web container (`Dockerfile.web` installs the
`github-app` extra), which `deploy/ec2/redeploy.sh` runs as container `app` on the
Docker network `web` behind Caddy, with its environment in `/etc/docs-drift.env`
(see [deployment](deploy.md)). After registering, on the server:

```text
sudo install -m 600 /dev/null /etc/docs-drift.env   # only if the file does not exist yet
sudoedit /etc/docs-drift.env                        # append the three lines the helper printed:
#   DOCS_DRIFT_GITHUB_APP_ID=<app id>
#   DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY=<base64 PEM>
#   DOCS_DRIFT_GITHUB_WEBHOOK_SECRET=<webhook secret>
DEPLOY_SHA=origin/main ./deploy/ec2/redeploy.sh       # recreates the container with the new environment
```

Docker env files take each value literally (no quotes or shell expansion), which is
why the key is base64-encoded. Then install the App on the repositories it should
review. The webhook URL is `https://13-211-179-95.sslip.io/github/webhook`; Caddy
forwards it to `app:8080`, and GitHub's "Recent deliveries" page shows the `202`
acknowledgements. Job workspaces live under the container's `/data` volume and
leftovers from a crash are removed at startup.

### Environment variables

| Variable | Required | Meaning |
| --- | --- | --- |
| `DOCS_DRIFT_GITHUB_APP_ID` | yes | Numeric App ID |
| `DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY` | yes | PEM, PEM with literal `\n`, or base64-encoded PEM |
| `DOCS_DRIFT_GITHUB_WEBHOOK_SECRET` | yes | At least 16 characters |
| `DOCS_DRIFT_GITHUB_API_URL` | no | API base URL (default `https://api.github.com`) |
| `DOCS_DRIFT_GITHUB_WORKERS` | no | Parallel PR jobs, 1–4 (default 2) |
| `DOCS_DRIFT_GITHUB_QUEUE_SIZE` | no | Queued PRs before deliveries get `503`, 1–256 (default 32) |
| `DOCS_DRIFT_GITHUB_HISTORY_DEPTH` | no | Commits fetched behind each tip, 1–2000 (default 200) |

Setting only some of the three required variables stops the service at startup
with an explicit error rather than silently running without the App.

## How a delivery is processed

1. `POST /github/webhook` verifies `X-Hub-Signature-256` (HMAC-SHA256 over the raw
   body, constant-time comparison) and returns `401` on a missing or wrong
   signature.
2. A repeated `X-GitHub-Delivery` ID within 24 hours is rejected with `409`
   (replay protection; the cache is bounded). Only accepted deliveries are
   remembered, so redelivering one that got `400` or `503` works.
3. The delivery is acknowledged with `202` immediately; work runs on a bounded
   thread pool. There is at most one pending job per PR: a newer push replaces the
   pending job, and a running job for an older commit finishes without publishing
   (its Check Run is concluded as superseded). A full backlog answers `503` so the
   delivery can be redelivered from GitHub.
4. The job fetches exactly the head and base commits with the installation token
   (passed to Git through environment configuration, never in argv or on disk),
   with no hooks, LFS smudge filters, submodules or redirects, a disk and time
   budget, a sparse checkout of text sources only, and symlinks materialized as
   plain files.
5. A disposable subprocess, which has no token or App key in its environment,
   loads the repository's own `.docs-drift.toml` (validated TOML data; model calls
   are always disabled), runs the PR comparison and writes a JSON bundle.
6. The parent publishes the Check Run, sticky summary and inline suggestions,
   then deletes the workspace. Any unexpected failure still concludes the Check Run
   (neutral, "could not finish") instead of leaving it in progress.

The App only trusts and edits comments written by its own bot account
(`<app-slug>[bot]`); a marker pasted into someone else's comment is ignored.

Installation tokens are cached per installation and refreshed five minutes before
expiry. App JWTs are RS256, backdated 60 seconds and expire in under ten minutes.

## Privacy

- Repository contents are fetched only for repositories where the App is
  installed, kept in a per-job workspace that is deleted when the job ends, and
  never sent to a model or any third party. Only GitHub is contacted.
- Logs contain the repository name, PR number, commit SHA, finding counts and the
  publishing actions, never tokens, keys, file contents or comment bodies.
- Comments contain short documentation and source excerpts from the repository
  itself, escaped and redacted with the same secret masking as every report.

## Limits

- github.com only (GitHub Enterprise Server needs a different clone URL).
- Historical snapshot budgets apply (64 MB archive, 128 MB expanded), as for
  `docs-drift pr`; files over 1 MB, lock files and non-text files are not checked.
- The queue is in memory. On shutdown (for example a redeploy) queued jobs are not
  run: they are logged, and running and queued pushes get a neutral "Service
  restarting" Check Run asking for `/docs-drift recheck` (shutdown waits about 15
  seconds, so a check can occasionally be left for the recheck to replace).
- See [security](SECURITY.md#github-integration-and-app) for the threat model.
