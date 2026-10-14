"""A pack's files, read off the disk: the manifest (`APP.yml`) and the
playbook files beside it (`<name>/PLAYBOOK.yml`, and the parts an entry
runs), placeholders filled, each parsed by `spec.specfile.load_yaml`.
The one place the conductor opens a pack file for its grammar.
"""

from pathlib import Path
from typing import Any

from physiclaw.common import paths
from physiclaw.common.paths import PACK_FILENAME, PLAYBOOK_FILENAME, RESERVED_PACK_DIRS
from physiclaw.common.placeholders import placeholder_values
from physiclaw.common.text import read_text
from physiclaw.conductor.spec.manifest import check_manifest_keys, check_manifest_meta
from physiclaw.conductor.spec.model import PlaybookError
from physiclaw.conductor.spec.specfile import bind, load_yaml, yaml_loader


def load_pack_doc(
    app: str,
    error_cls: type[ValueError],
    *,
    root: Path | None = None,
    values: dict[str, str] | None = None,
) -> dict | None:
    """`playbooks/<app>/APP.yml`, the manifest, loaded and
    top-checked: a mapping with only the known sections, no unpopulated
    template placeholders — or empty, which is a valid manifest (the
    file is the pack marker; every section is optional). None when the
    file doesn't exist (not a pack). Section contents are validated by
    their owners (`pages.py`, `model.py`) — this is just the shared
    front door."""
    path = (paths.pack_root(app) if root is None else root) / PACK_FILENAME
    if not path.exists():
        return None
    data = load_yaml(
        read_text(path), error_cls, where=f"{app}/{PACK_FILENAME}", values=values
    )
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise error_cls(f"{app}/{PACK_FILENAME} must be a YAML mapping")
    check_manifest_keys(data, app, error_cls)
    check_manifest_meta(data, app, path.parent.name, error_cls)
    return data


def load_playbook_docs(
    app: str,
    error_cls: type[ValueError],
    root: Path | None = None,
    values: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Every playbook of a pack as raw documents, plus what would not
    load, by id with the reason. An ENTRY is `<app>/<name>/PLAYBOOK.yml`,
    one folder per entry beside the manifest, keyed by the folder name
    (referenced as `<app>/<name>`); any other `<name>.yml` in that
    folder is a playbook the entry runs, keyed `<entry>.<name>`
    (referenced as `<app>/<entry>.<name>`) and listed right after it. A
    bad file excludes itself, never the pack: `scan_playbooks` reports
    it as an invalid entry. Folder and file names follow the move-name
    grammar, so one the grammar refuses is an error entry too; a `_` or
    `.` prefix is the one skip convention every artifact lister shares
    (a draft the author parks beside the pack). Strays are error entries
    so they never vanish silently: a `*.yml` at pack level that is not
    the manifest (a route that belongs in a folder, keyed by its file
    name so it can never shadow the folder it should be), a folder with
    no PLAYBOOK.yml that is not `macros/` or `prompts/`, and a folder
    inside an entry that is not one of those two (what an entry runs is
    a file). `root` is the pack's directory when the caller already
    resolved it; `values` the placeholder values when the caller read
    them (one read per pack load)."""
    root = paths.pack_root(app) if root is None else root
    name_check = bind(error_cls)[3]
    if values is None:
        try:
            values = placeholder_values()
        except ValueError as e:
            raise error_cls(str(e)) from e
    docs: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def read(playbook: str, path: Path, role: str) -> None:
        """One file into `docs` under its id, or into `errors` with the
        reason — the id's own name checked against the move-name
        grammar (the folder's for an entry, the file stem for a playbook
        it runs)."""
        where = f"{app}/{paths.playbook_file(playbook)}"
        try:
            name_check(paths.own_name(playbook), f"{where}: {role}")
            data = load_yaml(read_text(path), error_cls, where=where, values=values)
            if not isinstance(data, dict):
                raise error_cls(
                    f"{where} must be a YAML mapping (name, description, enabled, "
                    "inputs, route)"
                )
        except Exception as e:  # broad: exclude the file, never the pack
            errors[playbook] = str(e) or type(e).__name__
            return
        docs[playbook] = data

    for stray in paths.leaf_files(root, paths.PLAYBOOK_SUFFIX):
        if stray.name != PACK_FILENAME:
            errors[stray.name] = (
                f"{app}/{stray.name}: a playbook is a folder — move it to "
                f"{stray.stem}/{PLAYBOOK_FILENAME}"
            )
    for folder in paths.leaf_dirs(root):
        name = folder.name
        if name in RESERVED_PACK_DIRS:
            continue
        path = folder / PLAYBOOK_FILENAME
        if not path.is_file():
            errors[name] = (
                f"{app}/{name}/: a folder with no {PLAYBOOK_FILENAME} is not a playbook"
            )
            continue
        read(name, path, "entry folder name")
        for sibling in paths.leaf_files(folder, paths.PLAYBOOK_SUFFIX):
            if sibling.name != PLAYBOOK_FILENAME:
                read(f"{name}.{sibling.stem}", sibling, "playbook file name")
        for sub in paths.leaf_dirs(folder):
            if sub.name not in RESERVED_PACK_DIRS:
                # Keyed by the offending PATH, as the pack-level stray is:
                # a playbook of the same name may be valid beside it, and
                # must not be reported invalid in its place.
                errors[f"{name}/{sub.name}/"] = (
                    f"{app}/{name}/{sub.name}/: what an entry runs is a file "
                    f"beside its {PLAYBOOK_FILENAME} — "
                    f"{paths.playbook_file(f'{name}.{sub.name}')}"
                )
    return docs, errors


def read_template_manifest(src: Path) -> dict:
    """A template pack's APP.yml, loaded RAW (its `<<TOKEN>>`s still
    in place) for `playbooks install` — name/description plus the
    `placeholders:` spec map that drives the prompts. {} when absent;
    loud PlaybookError on bad YAML or a malformed `placeholders:`
    section, so install fails BEFORE it mutates anything."""
    path = src / PACK_FILENAME
    if not path.exists():
        return {}
    try:
        data = yaml_loader.load(read_text(path))
    except Exception as e:  # loader errors are not confined to YAMLError
        raise PlaybookError(f"{path}: invalid YAML: {e or type(e).__name__}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PlaybookError(f"{path} must be a YAML mapping")
    spec = data.get("placeholders") or {}
    if not isinstance(spec, dict) or not all(
        isinstance(v, dict) for v in spec.values()
    ):
        raise PlaybookError(
            f"{path}: `placeholders` must map TOKEN -> {{description, example}}"
        )
    return data
