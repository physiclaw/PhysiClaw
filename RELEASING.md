# Python releases

Start with committed changes and a clean checkout. Use the exact `uv` version in
`[tool.uv].required-version` in `pyproject.toml` (also checked against CI by tests).
An incompatible toolchain or stale lockfile stops the bump before any edits.
Replace `X.Y.Z` with the next version, or omit `VERSION` for a patch bump:

```sh
git switch main
git pull --ff-only
make bump VERSION=X.Y.Z
git push origin main
make release-check
make release-start
```

`make bump` commits only a version change in the project and lockfile. Dependency
or lockfile-format changes are rejected; a failed or interrupted lock generation
restores both original files before staging. Commit dependency updates separately.

`make release-check` requires `main` to match `origin/main`, a current lockfile,
and a successful latest **push** CI run for that exact commit. It creates no tag
and publishes nothing. If CI is pending, wait and repeat the check; if it failed,
inspect and fix the failure first. `make release-start` repeats all these checks,
then pushes an annotated version tag for the checked commit. CI independently
verifies the tag's type, version and target before building.
Both fetch and push URLs for `origin` must point to the official repository.
`make release` and `make publish` remain aliases. Pull requests are optional unless required by repository rules.

If the system `uv` is older, a temporary pinned toolchain can run any Make target:

```sh
uvx --from uv==0.13.7 uv run --no-project make bump
```

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
The tag push is only a release request: report completion only after the release
run succeeds and both PyPI and the published GitHub release contain the expected
wheel and source archive. Use a checkout of the tagged commit for approval if
`main` has advanced; `RUN_ID` never bypasses the version/commit checks.

## Versions and configuration

- `pyproject.toml` owns the version. Use `X.Y.Z` or `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`.
  Versions must increase; omitting `VERSION` bumps a stable version's patch number.
- Use patch releases for fixes and minor releases for features. Breaking changes
  require a minor release before 1.0 and a major release afterward.
- PyPI Trusted Publisher: `physiclaw/PhysiClaw`, workflow `release.yml`, environment
  `pypi`. Require deployment review and restrict deployments to `v*` tags.
- Build dependencies use the `build` group and `uv.lock`; CI generates temporary
  constraints. After changing dependencies, run `uv lock` and commit both files.
- CI requires fresh-install and upgrade checks before publication. The build
  selects the newest available, non-yanked stable PyPI release older than the
  candidate and records `upgrade_from` in `manifest.json` for all platform jobs.
  A failed lookup or no eligible baseline fails the build.

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
- If a failed tagged run needs a source or workflow fix, commit the fix and bump
  to a new patch version. Never move/delete the failed public tag to reuse it.
- The release validation checkout must use `ref: ${{ github.sha }}` with full
  history. Checkout's default tag-plus-commit fallback can replace an annotated
  tag with a lightweight local tag; regression tests protect this setting.
