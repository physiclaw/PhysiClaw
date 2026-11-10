# Releasing the Python package

Software releases use `vX.Y.Z` tags. GitHub Actions runs the complete CI suite,
builds a wheel and source archive once, tests installation of both on Linux,
macOS and Windows, and publishes those same files through PyPI Trusted Publishing.
Every uploaded file receives a PyPI publish attestation. Publication then verifies
PyPI's file bytes and attestations and performs a fresh installation from PyPI.

Hardware releases retain their independent `physiclaw-hardware-vX.Y` pipeline.
Software GitHub releases use `--latest=false` because hardware downloads currently
depend on the repository's `releases/latest` URL.

## One-time maintainer setup

Complete this before pushing the first software release tag:

1. In GitHub Settings → Environments, create **`pypi`**. Restrict deployments to
   selected **tags** matching `v*`; do not allow branch deployments. Add a trusted
   maintainer as a required reviewer. A sole maintainer must be allowed to approve
   their own run; teams can enable prevention of self-review. Disable administrator
   bypass where appropriate. These settings live in GitHub, not in the YAML.
2. In [PyPI → physiclaw → Publishing](https://pypi.org/manage/project/physiclaw/settings/publishing/),
   add a GitHub Trusted Publisher with these exact values:

   | Field | Value |
   | --- | --- |
   | Owner | `physiclaw` |
   | Repository | `PhysiClaw` |
   | Workflow filename | `release.yml` |
   | Environment | `pypi` |

   Register the top-level release workflow, not `ci.yml` or `package.yml`.
3. Protect `main` with PR review and required CI checks. Protect `v*` tags from
   updates and deletion, and restrict their creation to release maintainers.
   Require review for `.github/workflows/`, release scripts, and build constraints.
4. Run **Release → Run workflow** on the migration branch or `main` for a dry run.
   `workflow_dispatch` builds and tests but never publishes, even when run on a tag.
   Inspect the package installation jobs and downloadable artifact bundle.
5. After the first successful attested release, revoke obsolete PyPI API tokens
   and remove `UV_PUBLISH_TOKEN` / `PYPI_API_TOKEN` from release environments.
   No package publishing secret is needed by this pipeline.

Use 2FA for maintainer accounts and keep recovery codes securely. Action references
are pinned to commit SHAs; Dependabot proposes weekly updates. The uv version and
release tool versions are explicit. `scripts/release-build-constraints.txt` pins
the build backend and its dependency hashes from `uv.lock`; refresh it when those
build dependencies change.

## Version policy and release preparation

`pyproject.toml` is the version source. `uv.lock` records the same project version;
the installed package reads its version from distribution metadata. Do not edit
another runtime version constant.

- Use `X.Y.Z` for stable releases and `X.Y.ZaN`, `X.Y.ZbN`, or `X.Y.ZrcN` for
  prereleases, without leading zeros or a `v` in package metadata.
- Patch releases fix compatible behavior. Minor releases add features; while the
  project is below 1.0, document breaking changes prominently in minor releases.
  After 1.0, use a major version for breaking public behavior.
- Version bumps must increase according to this policy. A release candidate can
  advance to the next candidate or the corresponding stable version. Development,
  local, epoch and post-release versions are intentionally outside this workflow.
- `make bump` increments the patch of a stable version. Prereleases require an
  explicit `VERSION`; promoting `0.12.0rc1` means `VERSION=0.12.0`.

Prepare the version on a branch:

```sh
git switch -c release/0.11.1
make bump VERSION=0.11.1
make build  # optional local check; these local files are never uploaded
```

`make bump` requires a clean checkout, updates the version and lockfile, and
commits both locally. Review the diff, add release notes to the PR, push the branch,
and merge after CI passes. Record hardware/firmware compatibility and real-device
acceptance results when changing motion, calibration, cameras, or firmware behavior.
The automated package checks do not operate real hardware.

## Publish a merged version

After merging the release PR:

```sh
git switch main
git pull --ff-only
make release
```

`make release` (also available as `make publish`) checks a clean `main`, the official
origin, equality with remote `main`, and absence of the tag locally and remotely.
It creates and pushes an annotated tag. It does **not** bump a version, push main,
upload a local build, or claim the package has already been released.
`make release VERSION=...` is rejected: prepare that change in a PR first.

In the Release run:

1. The tag must match the package version and its commit must belong to `main`.
2. Reusable CI runs the existing tests, coverage, lint and type checks. Its Package
   workflow builds the sdist and then the wheel from that sdist, checks metadata,
   licenses/resources and records SHA-256 hashes in `manifest.json`.
3. Fresh environments install each artifact on all three operating systems,
   resolve dependencies as users do, check dependency consistency, and exercise
   imports, resources and the CLI. Python 3.12 is the current installer/CI baseline;
   these checks do not establish compatibility with every newer Python release.
4. A prepublication check rejects conflicting, yanked or unattested existing files.
   Approve the `pypi` deployment after reviewing the successful checks and artifacts.
5. The publisher only downloads the immutable artifact ID and uploads the two
   distributions. It has OIDC permission but no checkout or project execution.
6. A separate job downloads and hashes PyPI's files, cryptographically verifies
   both attestations, and smoke-tests a fresh PyPI installation. Only then does
   the workflow create GitHub release notes and attach the distributions/manifest.

Check that the PyPI Security tab shows provenance on **both files**. A publish
attestation identifies the publishing workflow; it is not a claim of vulnerability
freedom or a separate SLSA build attestation.

Docs, skills and installer mirroring remain independent workflows. Merge content
requiring a new stable package after its publication succeeds; compatible content
fixes can ship independently. A failed docs deployment must not republish a package.

## Failure and retry policy

- Before publication, fix failures in a new reviewed commit/version as needed.
  Never move a public release tag to different code.
- If publication is interrupted, use **Re-run failed jobs** on the original run.
  This retains the successful build's artifact ID and exact bytes. Artifacts are
  retained for 30 days. Do not use **Re-run all jobs** to recover a partial upload:
  that rebuilds packages and may produce different bytes.
- Existing PyPI files must match the manifest and already have valid attestations.
  Only then may the publisher skip them and upload missing files. Conflicts stop
  the pipeline. A release must never combine files from different builds.
- If PyPI succeeded but verification or GitHub release creation failed, rerun only
  failed jobs. The PyPI release is already public; a red workflow does not undo it.
- If an artifact expired or existing files conflict, investigate and prepare a
  new version instead of deleting/replacing PyPI files or weakening verification.
- If a published release is broken, assess yanking it with an explanatory reason,
  then publish a corrected version. Yanking does not remove existing installations.
- If the tag push fails, inspect the remote tag and workflow before retrying. The
  helper leaves the local tag intact rather than silently deleting release state.

The existing `0.11.0` files stay as published. Introduce attestations with a new
version; old filenames cannot be replaced to retrofit provenance.

## References

- [PyPA's GitHub Actions publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [PyPI Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
- [Producing attestations](https://docs.pypi.org/attestations/producing-attestations/)
- [Verifying attestations](https://docs.pypi.org/attestations/consuming-attestations/)
