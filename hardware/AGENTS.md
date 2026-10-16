# Hardware build and release rules

- Read `hardware/README.md` before changing this pipeline. Source files are
  authoritative; do not edit generated files under `hardware/output/` to fix a build.
- Run `make hw-check` and `uv run --locked --group cad pytest tests/hardware`
  after hardware pipeline changes. CAD tests must run, not be skipped.
- For release preparation use `make hw-prepare HW_VERSION=X.Y`, then
  `make hw-verify HW_VERSION=X.Y`. Preparation is local and builds every
  procedure, document and print package in a private output tree. Reuse the
  validated persistent CAD cache; do not force a fresh CAD build for releases.
- Publish only when the user has requested publication. Use
  `make hw-publish HW_VERSION=X.Y` after committing and pushing main, or
  `make hw-release HW_VERSION=X.Y` to prepare and publish together. Both trigger
  the docs deploy hook after publication. If that hook fails, retry only
  `make docs-deploy`; do not recreate the release.
- Publication success requires reading back the published latest release, matching
  its assets to the verified draft, and resolving its actual Git tag to the bundle
  commit. Recheck after the docs hook. Never infer success from a publish command's
  exit code alone. The docs hook starts an asynchronous site build.
- Do not publish with ad hoc `gh release` commands, package interactive
  output directories, hand-edit manifests, ignore nonzero exits, or remove
  guards to get a release through. Fix the failing source or environment.
- A failed upload leaves a draft. Inspect the error, then retry the same command.
  Matching drafts resume by uploading missing assets; GitHub SHA-256 digests
  verify uploads (download only when absent). Conflicting drafts/assets stop
  publication. Never overwrite them; inspect stale drafts before removing them.
- Repeated preparation reuses only a fully verified bundle of the current commit
  and sources. Move stale generated bundles aside before preparing replacements.
  Published releases and existing tags are never overwritten.
- Source changes require a newly prepared bundle. The manual cover date in
  `documents/MANUAL_VERSION` is separate from the numeric hardware release version.
- Document sources live under `documents/`: `manual/` owns assembly instructions,
  `sourcing/` owns supplier data and optional tools, and `drawing/` owns technical
  drawing rendering. All three consume the authored purchasing list in
  `documents/bom/bom.json`; do not duplicate its rows in manual chapters or
  replace it with the separate CAD-derived per-step BOM. Preserve CLI commands,
  generated paths and release asset names when reorganizing these sources.
- Build checks verify software artifacts, not physical fit, tolerances or
  assembly safety. Geometry changes still require engineering review.
