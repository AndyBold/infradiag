"""Progress lines on stderr, so stdout stays clean for piped output.

On a terminal, counted steps share one updating line (\\r); otherwise every line is plain text so logs stay readable.
"""

from __future__ import annotations

import sys
from typing import TextIO


class Progress:
    def __init__(self, stream: TextIO | None = None):
        self.stream = stream if stream is not None else sys.stderr
        self.tty = self.stream.isatty()
        self._open = False  # a transient line is on screen, with no newline yet

    def line(self, message: str) -> None:
        """A permanent line, e.g. the start of a phase."""
        self.clear()
        print(message, file=self.stream, flush=True)

    def step(self, message: str, n: int, total: int) -> None:
        """Step n of total (1-based); the label gets an `n/total` counter. Transient on a terminal."""
        text = f"{message} {n}/{total}"
        if not self.tty:
            print(text, file=self.stream, flush=True)
            return
        self.stream.write(f"\r\033[K{text}")
        self.stream.flush()
        self._open = True

    def clear(self) -> None:
        """Wipe a transient line before anything else is written to the terminal."""
        if self._open:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self._open = False
