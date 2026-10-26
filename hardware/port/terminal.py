"""Terminal input with native mode restoration owned by a context manager."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from hardware.port._select import native as _native


@contextmanager
def key_input(stream: TextIO | None = None) -> Iterator[None]:
    with _native.key_input(sys.stdin if stream is None else stream):
        yield


def read_key(stream: TextIO | None = None) -> str:
    return _native.read_key(sys.stdin if stream is None else stream)
