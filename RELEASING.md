# Releasing docs-drift

Status: preparation only. No package, tag, image, GitHub Release, Marketplace
listing or hosting resource has been published by this release preparation.
Stop at every **PUBLISH GATE** below. The owner confirms each one.

## Version policy

Use **hatch-vcs**. A full SemVer tag is the single source of truth:
`v0.1.0-rc.1` becomes Python package `0.1.0rc1`; `v0.1.0` becomes `0.1.0`.
Minor/patch releases follow SemVer; describe breaking pre-1.0 changes explicitly.
Moving `v0` and `submission-2026-10-30` tags do not determine package versions.
Untagged source builds are development builds, never a disguised stable release.
Do not set SETUPTOOLS_SCM_PRETEND_VERSION in the publishing workflow.
CLI --version reads installed distribution metadata. JSON package_version has
that value; schema_version remains the independent integer 1 for the initial
public contract. Review schema changes separately; changes incompatible with a
published schema require a new schema version, independently of package version.

The README metadata hook rewrites images to raw GitHub URLs and file links to
absolute GitHub URLs. Inspect the built METADATA and twine output, not only the
repository README. See [hatch-vcs](https://github.com/ofek/hatch-vcs) and
[the README hook](https://github.com/hynek/hatch-fancy-pypi-readme).

JavaScript/TypeScript grammars are installed as the dedicated official grammar
wheels. A scan does not download grammars at runtime. A fresh-directory,
network-disabled test guards this first-install behavior; see
[Tree-sitter's wheel-loading guidance](https://github.com/tree-sitter/py-tree-sitter).

## One-time owner setup: PyPI and TestPyPI web UI

You must perform this yourself. No API token is needed or stored.

1. Sign in to your account at PyPI, enable its required account security, and
   open **Your account → Publishing**: https://pypi.org/manage/account/publishing/.
   For a project that does not exist, use **Add a new pending publisher** and
   select GitHub. A pending publisher does not reserve the package name.
2. Enter these exact values for production:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `docs-drift` |
   | GitHub owner | `ayushagarwalhere` |
   | Repository name | `docs-drift` |
   | Workflow filename | `release.yml` (not its directory path) |
   | Environment name | `pypi` |

3. Add the publisher. If you already own the project, instead open **Your projects
   → docs-drift → Manage → Publishing → Add a new publisher** with the same fields.
4. Independently sign in to TestPyPI and repeat at
   https://test.pypi.org/manage/account/publishing/. Use the same project, owner,
   repository and workflow, but the environment name must be **testpypi**.
   These sites have separate accounts/configuration. Do not upload a priming
   package manually; the pending trusted publisher can create the project.

These steps follow [PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [existing-project guide](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## One-time owner setup: GitHub gates

In this repository's **Settings → Environments**, create **testpypi**, **pypi**,
**ghcr**, and **github-release**. Add yourself as a required reviewer to each;
leave prevention of self-review off if you will approve your own releases.
Restrict deployment to version tags. Verify your GitHub plan supports required
reviewers; if it does not, leave publishing disabled and do not push a release tag.
In **Settings → Secrets and variables → Actions → Variables**, leave
DOCS_DRIFT_RELEASE_PUBLISH_ENABLED unset until local validation and your first
publish approval. Set it to `true` only after all four review gates are configured.
No PyPI secret is configured. The only id-token write permission is on the
isolated publishing job; build and test jobs do not receive it.

Enable GitHub Pages from the approved docs workflow; set DOCS_DRIFT_PAGES_ENABLED
only after reviewing its deployment. Make a published GHCR package public through
its package settings, then verify an anonymous pull. Keep the default branch main
at the reviewed final commit. Branch protection must also cover workflow changes.

## Local preparation, with no publication

1. Review the real incident, human labels, demo URL and video slots in SUBMISSION.md.
   Keep unfinished items explicitly pending; they block final submission claims,
   not an honest alpha release candidate. Do not replace pending accuracy with
   fixture numbers. Update CHANGELOG.md's
   0.1.0 date to the actual stable release date before the RC commit.
2. Run the tests, lint, type checks, dependency audit, own-repo check and history
   secret scan. Build a clean checkout of the reviewed work. Preserve the user's
   existing index; a local validation snapshot is not the remote default branch.
3. Build artifacts from the RC-tagged validation checkout with `uv build`.
   Run scripts/release.py check and `twine check --strict` on those artifacts.
   Test the installed wheel in a separate environment with no src directory,
   generate the HTML attachment and package the VSIX. Record hashes.
4. Review the exact source diff and commit. **PUBLISH GATE A:** ask before pushing
   the reviewed commit to main or pushing the release-candidate tag. A tag push
   starts the workflow; do not push it as an incidental validation step.

## First TestPyPI release candidate

Local build checks (run from a fresh reviewed checkout):

```sh
uv sync --locked --all-extras
uv build
uv run python scripts/release.py check --dist dist
uvx --from twine twine check --strict dist/*
```

The exact wheel-only test setup is in release.yml's **Test the wheel outside the
source tree** step: it creates a new uv environment, installs the built wheel
with test/runtime extras, stages test inputs without src, asserts site-packages
imports, then runs the full suite. Use a new staging directory each time.
Do not approve a tag based only on an editable-install test.

After your trusted-publisher setup and explicit push approval, the owner runs:

```sh
git tag -a v0.1.0-rc.1 -m "docs-drift 0.1.0 release candidate 1"
git push origin main
git push origin v0.1.0-rc.1
```

The workflow builds a wheel and sdist, checks contents and metadata, and runs
the full tests against the installed wheel outside the source tree.
**PUBLISH GATE B:** approve only the waiting **testpypi** job. It publishes through
OIDC to TestPyPI. No password or API token is passed to the PyPA action.

The 27 install jobs cover Ubuntu/macOS/Windows × Python 3.11/3.12/3.13 ×
pipx/uv tool/pip. For RCs, the exact TestPyPI wheel URL and SHA256 come from its
JSON API; dependencies resolve from real PyPI. This avoids mixing two dependency
indexes. The stable jobs install the named, pinned package from real PyPI.
Every job checks CLI version, a real fixture scan and both JSON versions.
See [PyPA's publishing action](https://github.com/pypa/gh-action-pypi-publish).

**PUBLISH GATE C:** after the matrix passes, approve **ghcr** to push the image
built from that same wheel. RC images get their exact RC tag, never latest.
**PUBLISH GATE D:** approve **github-release** to publish the prerelease with
the wheel, sdist, VSIX, example HTML and SHA256SUMS attached.
The VSIX is never sent to the Marketplace. Install it locally using Extensions
→ Install from VSIX, with docs-drift's lsp extra installed separately.

## Stable release

Do not change source between the successful RC and the stable tag. The workflow
checks that a successful RC Release run exists at that exact commit. If anything
changes, make a new numbered RC and repeat validation.
After explicit owner approval, tag that same commit:

```sh
git tag -a v0.1.0 -m "docs-drift 0.1.0"
git push origin v0.1.0
```

Approve the **pypi**, **ghcr**, and **github-release** jobs separately, in that
order. Stable installs are verified against real PyPI before image/release jobs.
Check `pipx install docs-drift` from a fresh home and scan a pinned public repo.
Check an anonymous GHCR pull, release attachments and PyPI image rendering.
Never delete/reuse a published version to fix an artifact; issue a new version.

The GitHub Action is available under the same full release tag. The owner alone
updates the moving compatibility tag after successful validation:

```sh
git tag -fa v0 v0.1.0 -m "docs-drift v0 compatibility tag"
git push origin refs/tags/v0 --force
```

This intentionally moves v0; do not force-update any full release tag. Consumers
needing immutable builds should pin the full commit SHA. Later releases repeat
the RC/stable tag flow; there is no manual version-file edit or artifact upload.

## Judging freeze: 30 October through 6 November 2026 inclusive

Do not push breaking changes to the deployed demo. Work on a branch; keep the
judged production image pinned to its approved commit/digest. The deployment
workflow has a UTC check for the full IST freeze window and also honors
DOCS_DRIFT_DEPLOY_FREEZE. Set that variable to `true` before submission and keep
it on through 6 November. Only an explicitly approved emergency rollback should
alter production. Keep monitoring active through 6 November, with alerts reviewed
by a human. GitHub scheduled runs are best effort, not an uptime SLA.

Web container builds must receive PACKAGE_VERSION from the exact approved
version tag; the source-only Docker context has no .git directory. The deployment
workflow derives it from that tag; untagged commits are not production releases.
Only this non-Git container build uses the explicit version override. Package
publication still derives its version directly from Git without an override.

After submission approval, tag the actual submitted main commit
submission-2026-10-30 and push that tag only with a separate confirmation.
Score questions can be raised through 5 November; winners are announced 6 November.
