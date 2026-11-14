# Python releases

Use a clean checkout with Git, uv, GNU Make and authenticated `gh` available.
`make bump` creates a commit; agents need explicit permission before running it.

```sh
git switch main
git pull --ff-only
make bump
git push origin main
make release-check
make release-start
make release-status
make release-approve
make release-watch
```

`make bump` increments the patch, or accepts `VERSION=X.Y.Z`. It rejects dependency
or lockfile-format changes and restores both files if lock generation fails.
CI pins uv for reproducibility; local installations need not match. If a bump
rewrites unrelated lockfile data, retry with CI's uv without replacing your
installation: `uvx --from uv==0.13.7 uv run --no-project make bump`.
This also works on Windows with GNU Make installed; replace `bump` for other targets.

`release-check` requires an up-to-date lockfile, `main` matching `origin/main`,
official fetch/push URLs, and successful push CI for that commit. Wait for pending
CI; inspect failures before retrying. `release-start` repeats these checks and
pushes an annotated tag. `release` and `publish` are aliases.

Approve only when publication is authorized. Status, approval and watch use this
checkout's version and commit; if `main` has advanced, use the tagged checkout.
`RUN_ID=ID` selects a run without bypassing these checks; `JSON=1` gives structured
status/approval output. A manual **Run workflow** is a non-publishing dry run.

Completion requires successful release CI, verified PyPI artifacts and a published
GitHub release with its wheel and source archive. Keep GitHub's hardware `latest`
release unchanged.

Post-publication verification uses a five-minute polling budget for both verified
files to appear in PyPI's installation index before testing a fresh install. Missing
index entries and transient network errors are retried; conflicting hashes,
yanked files and invalid attestations fail verification.

## Configuration

- Versions increase and use `X.Y.Z`, `X.Y.ZaN`, `X.Y.ZbN` or `X.Y.ZrcN`. Use patch
  for fixes, minor for features, and major for breaking changes (minor before 1.0).
- PyPI Trusted Publisher: `physiclaw/PhysiClaw`, workflow `release.yml`, environment
  `pypi`. Require deployment review and restrict deployments to `v*` tags.
- Build dependencies live in the `build` group and `uv.lock`. Update them separately.
  CI tests fresh installs and upgrades from the latest eligible stable PyPI version
  on Linux, macOS and Windows before publication.

## Recovery

- A failed tag push can be retried from the same clean, pushed commit while the
  remote tag is absent. Never move or delete a public tag; source fixes, conflicting
  artifacts or expired artifacts require a new version.
- Retry transient failures with **Re-run failed jobs** on the original run. Its
  artifacts remain available for 30 days; retries verify hashes and attestations.
  GitHub uploads resume in a draft until all assets are verified.
- For a broken published release, yank it if needed and release a fix. Preserve
  the CI, approval and artifact checks throughout recovery.
