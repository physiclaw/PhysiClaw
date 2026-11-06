# Hardware instructions

Read [README.md](README.md) for setup and common commands. Run commands from the
repository root; paths below are relative to that root.

## Edit sources

- Fix source files, then regenerate outputs. Do not fix builds by editing files
  under `hardware/output/`.
- Preserve CLI commands, generated paths and release asset names when
  reorganizing sources.
- Under `hardware/documents/`, `manual/` contains assembly instructions,
  `sourcing/` contains suppliers and optional tools, and `drawing/` renders
  technical drawings. See the [document source guide](documents/README.md).
- All three documents use `hardware/documents/bom/bom.json` as the purchasing
  list. Do not copy its rows into chapters or replace it with CAD-generated
  per-step parts lists.
- `hardware/documents/MANUAL_VERSION` sets the manual cover date. It is separate
  from the numeric hardware release version and is updated at the user's discretion.

## Check pipeline changes

Run both commands after hardware pipeline changes. CAD tests must run without
being skipped:

```sh
make hw-check
uv run --locked --group cad pytest tests/hardware
```

These checks cover source consistency and software behavior. Geometry changes also
need engineering review for physical fit, tolerances and assembly safety.

## Prepare a release locally

For a release intended for publication, commit and push `main` before preparing.
Replace `X.Y` with the hardware version. Chrome or Chromium is required:

```sh
make hw-prepare HW_VERSION=X.Y
make hw-verify HW_VERSION=X.Y
```

Preparing a new bundle runs the hardware tests and consistency checks, then
generates every assembly step, document and print package in a private output
directory. Preparation uploads nothing. Reuse the validated CAD cache; do not
force a fresh CAD build for a release or package files from ordinary local builds.

An existing bundle can be reused only after verification against the current
commit and sources. If sources change, inspect and move the old bundle aside,
then prepare again. Do the same for damaged bundles. Do not edit manifests by hand.

## Publish only when the user requests it

- Publish a prepared bundle with `make hw-publish HW_VERSION=X.Y`. To prepare
  and publish together, use `make hw-release HW_VERSION=X.Y`.
- Publishing requires a clean `main` checkout matching live `origin/main` and an
  authenticated GitHub CLI (`gh`).
  Use these guarded commands; do not publish through ad hoc `gh release` calls,
  ignore failures or remove checks to force a release through.
- Confirm publication by reading back GitHub's latest release, matching its
  assets to the verified draft, and resolving the actual Git tag to the bundle's
  commit. Check again after the docs deployment hook. An exit code alone is
  insufficient.
- Both publish commands trigger the docs deployment hook. The site build runs
  asynchronously; an accepted hook does not mean the updated site is live.

## Recover from release failures

- **Upload failed:** inspect the error, then retry the same command. A matching
  draft resumes with missing assets only. Verify uploads using GitHub SHA-256
  digests; download assets for verification only when those digests are absent.
- **Draft or assets conflict:** stop and inspect them. Never overwrite conflicts,
  published releases or existing tags. Inspect stale drafts before removing them.
- **Docs hook failed after publication:** retry only `make docs-deploy`.
  The release is already published; do not recreate it.
