# Hardware build and release rules

- Read `hardware/README.md` before changing this pipeline. Source files are
  authoritative; do not edit generated files under `hardware/output/` to fix a build.
- Run `make hw-check` and `uv run --locked --group cad pytest tests/hardware`
  after hardware pipeline changes. CAD tests must run, not be skipped.
- For release preparation use `make hw-prepare HW_VERSION=X.Y`, then
  `make hw-verify HW_VERSION=X.Y`. Preparation is local and builds every
  procedure, document and print package in an empty private output tree.
- Publish only when the user has requested publication. Use
  `make hw-release HW_VERSION=X.Y` after committing and pushing main, or
  `make hw-deploy HW_VERSION=X.Y` to prepare and publish together.
- Do not publish with ad hoc `gh release` commands, package interactive
  output directories, hand-edit manifests, ignore nonzero exits, or remove
  guards to get a release through. Fix the failing source or environment.
- A failed upload leaves a draft. Inspect the draft and error before retrying;
  the release workflow deliberately refuses to overwrite existing releases,
  drafts, tags or prepared version directories.
- Source changes require a newly prepared bundle. The manual cover date in
  `manual/MANUAL_VERSION` is separate from the numeric hardware release version.
- Build checks verify software artifacts, not physical fit, tolerances or
  assembly safety. Geometry changes still require engineering review.
