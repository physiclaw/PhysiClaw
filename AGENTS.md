# Software releases

Read `RELEASING.md` before making a software release. A request to make a release
authorizes the normal publication steps below; do not stop at a version edit or
tag push and call it published.

- Use the exact uv version required by `pyproject.toml`. Use `make bump` for patch
  increments, or pass the user's explicit `VERSION`. Do not hand-edit the lockfile
  or combine dependency upgrades with a version bump.
- Start from a clean, up-to-date `main`. Review the bump commit, push it, and wait
  for `make release-check` to pass. `make release-start` repeats preflight and is
  the only normal entry point for pushing a software release tag.
- When CI fails, inspect its failed steps. Rerun the existing run only for a
  transient failure; source/workflow fixes require a new commit and patch tag.
  Never move or delete an already-pushed software version tag.
- Keep the annotated-tag, source-commit, CI, package, environment-review and
  attestation checks enabled. Do not publish locally or bypass CI to recover a
  failed release.
- When publication is authorized and the checks pass, use `make release-approve`
  from the tagged checkout, then follow `make release-watch`. `RUN_ID` selects a
  run but does not allow approving a different version or commit.
- Verify successful CI, PyPI artifacts, and a non-draft GitHub release before
  reporting completion. Keep software releases from changing GitHub's hardware
  `latest` release.
