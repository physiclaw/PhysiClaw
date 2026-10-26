"""In-memory consoles with strict encoding, usable without pytest fixtures."""

import io


class Console(io.StringIO):
    def __init__(self, encoding="utf-8", *, tty=True):
        super().__init__()
        self.codec = encoding
        self.tty = tty

    @property
    def encoding(self):
        return self.codec

    def isatty(self):
        return self.tty

    def write(self, text):
        text.encode(self.encoding, "strict")
        return super().write(text)
