# Python releases

Start with committed changes and a clean checkout. Replace `X.Y.Z` with the next version:

```sh
git switch main
git pull --ff-only
make bump VERSION=X.Y.Z
git push origin main
make release-start
```

`make bump` commits the version and lockfile locally. `make release-start` requires
`main` to match `origin/main`, then pushes an annotated version tag for that checked
commit. CI independently verifies the tag's type, version and target before building.
Both fetch and push URLs for `origin` must point to the official repository.
`make release` and `make publish` remain aliases. Pull requests are optional unless required by repository rules.

Inspect progress, approve when checks pass, then follow publication:

```sh
make release-status
make release-approve
make release-watch
```

These commands use `gh` and find the run matching this version and commit.
Use `RUN_ID=ID` to select a run explicitly; `JSON=1` gives structured status/approval output.
Approval requires a matching checkout and an authorized reviewer, and allows CI to publish to PyPI.
Agents approve only when publication is authorized. CI verifies attestations and
downloads before creating the GitHub release. **Run workflow** is a non-publishing dry run.

## Versions and configuration

- `pyproject.toml` owns the version. Use `X.Y.Z` or `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`.
  Versions must increase; omitting `VERSION` bumps a stable version's patch number.
- Use patch releases for fixes and minor releases for features. Breaking changes
  require a minor release before 1.0 and a major release afterward.
- PyPI Trusted Publisher: `physiclaw/PhysiClaw`, workflow `release.yml`, environment
  `pypi`. Require deployment review and restrict deployments to `v*` tags.
- Build dependencies use the `build` group and `uv.lock`; CI generates temporary
  constraints. After changing dependencies, run `uv lock` and commit both files.

## Recovery

- If the tag push fails, rerun `make release-start` from the same clean, pushed
  commit. It reuses the local annotated tag only when it points directly to HEAD
  and the remote tag is absent. If the remote tag already exists, inspect or rerun
  its workflow instead; the command never replaces published tags.
- Use **Re-run failed jobs** on the original run to reuse its artifacts (retained
  for 30 days). Retries require matching hashes and verified attestations.
- GitHub releases stay drafts until all assets upload; retries resume missing assets and verify existing bytes.
- Keep public tags fixed. Conflicting files or expired artifacts require a new
  version. For a broken published release, yank it if needed and release a fix.
