"""Repository hardware toolchain; independent of the installed application."""

from hardware.port.desktop import prepare_fonts as _prepare_fonts

_prepare_fonts()  # Must precede any build123d/OCCT import.
