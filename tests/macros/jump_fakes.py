"""The jump's test fakes, shared by the parser and runner tests: a
macro with one jump, and a page resolver standing in for the
conductor's (`match.PageCheck`) — the runner only sees a `Clause`."""

from dataclasses import dataclass

from physiclaw.macros.model import Clause, MacroError, Screen

JUMP = """name: send
description: reach the thread unless already there, then type
steps:
  - if_page: thread
    goto: type
  - home_screen
  - tap: "the chat"
    at: [0.1, 0.2, 0.9, 0.3]
  - mark: type
  - send_to_clipboard: "hi"
"""


@dataclass(frozen=True)
class FakePage(Clause):
    """A page read as one text on screen, displayed like the real one."""

    name: str
    text: str

    def holds(self, screen: Screen) -> bool:
        return self.text in screen.content

    def display(self) -> str:
        return f"page {self.name}"

    def substituted(self, values: dict[str, str]) -> Clause:
        return self


def pages(name: str) -> Clause:
    if name == "thread":
        return FakePage(name="thread", text="Thread")
    raise MacroError(f"no page {name!r} in pack 'demo' — it declares: thread")
