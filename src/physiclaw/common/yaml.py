"""YAML 1.2 for authored packs, without alias expansion.

Reject aliases while composing nodes, before the safe constructor can
expand merge keys. Checking the constructed Python tree is too late:
repeated merges can do exponential work and leave no shared identities.
"""

from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.composer import Composer, ComposerError
from ruamel.yaml.events import AliasEvent


class _NoAliasComposer(Composer):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.parser.check_event(AliasEvent):
            event = self.parser.get_event()
            raise ComposerError(
                None,
                None,
                "YAML anchors/aliases (`&name` / `*name`) are not supported — "
                "write the value out in full",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def safe_yaml() -> YAML:
    """A pure-Python safe loader/dumper that rejects aliases before expansion."""
    yaml = YAML(typ="safe", pure=True)
    yaml.Composer = _NoAliasComposer
    return yaml
